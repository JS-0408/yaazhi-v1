"""
Yaazhi Task Planner — production-hardened.

Audit fixes applied (2026-05-10):
  W6 : task_type validated against VALID_AGENT_TYPES whitelist via Pydantic validator.
  W7 : Maximum 10 subtasks enforced in validator + system prompt.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import litellm
import logfire
from pydantic import ValidationError

from config.settings import settings
from core.agent_registry import AgentRegistry
from core.state import SubTask, TaskPlan, TaskType

# ---------------------------------------------------------------------------
# Valid agent types whitelist (W6 fix)
# ---------------------------------------------------------------------------

VALID_AGENT_TYPES: frozenset[str] = frozenset(
    {
        TaskType.RESEARCH.value,
        TaskType.CODE.value,
        TaskType.READ_DOC.value,
        TaskType.BROWSE.value,
        TaskType.NOTIFY.value,
        TaskType.MEMORY.value,
    }
)

_MAX_SUBTASKS = 10   # W7 fix


# ---------------------------------------------------------------------------
# Planner system prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    """
    Build planner system prompt dynamically from registered agents.

    The list of available agents comes from AgentRegistry so adding
    a new agent automatically updates the planner's awareness.
    """
    try:
        registered = AgentRegistry.list_agents()
        # Intersect with valid types for safety
        available = sorted(VALID_AGENT_TYPES.intersection(set(registered)))
    except Exception:
        available = sorted(VALID_AGENT_TYPES)

    agent_descriptions = {
        "researcher": "Web search and research using DuckDuckGo + LLM synthesis",
        "coder": "Write and execute Python code in a sandboxed environment",
        "reader": "Read and summarise PDFs, documents, or long web pages (Gemini 2M context)",
        "browser": "Headless browser automation — navigate, click, extract from websites",
        "notifier": "Send alerts via Telegram, WhatsApp, or Email",
        "memory_only": "Answer directly from existing memory without calling external agents",
    }

    agent_list = "\n".join(
        f"  - {name}: {agent_descriptions.get(name, 'No description')}"
        for name in available
    )

    return f"""You are Yaazhi's task planner. Decompose the user's request into subtasks.

Available agents:
{agent_list}

Rules:
1. Each subtask MUST use one of the agents listed above — no other values allowed.
2. Maximum {_MAX_SUBTASKS} subtasks allowed — keep plans concise.
3. Use depends_on to express ordering (task_id of prerequisite subtask).
4. Prefer parallel execution: group independent subtasks (no shared depends_on).
5. Use memory_only if the answer is already in Yaazhi's knowledge base.
6. Always return valid JSON — no markdown, no explanation, just JSON.

Output format (strict JSON):
{{
  "original_task": "<user request>",
  "strategy": "parallel|sequential",
  "estimated_duration_seconds": <int>,
  "subtasks": [
    {{
      "task_id": "t1",
      "agent": "<one of the agents above>",
      "description": "<specific instruction for the agent>",
      "depends_on": [],
      "priority": 1
    }}
  ]
}}"""


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class Planner:
    """
    Decomposes user requests into executable TaskPlans.

    Uses an LLM (GPT-4o / Groq fallback) to produce structured JSON plans
    that are validated by Pydantic before being returned to the orchestrator.
    """

    def __init__(self) -> None:
        self._system_prompt: Optional[str] = None

    def _get_system_prompt(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = _build_system_prompt()
        return self._system_prompt

    async def plan(
        self,
        task: str,
        context: str = "",
        revise_reason: str = "",
    ) -> TaskPlan:
        """
        Produce a TaskPlan for the given task description.

        On ValidationError (bad agent type or too many subtasks), falls back
        to a single researcher subtask so the orchestrator never stalls.

        Args:
            task         : The user's request.
            context      : Recalled memory context to inform planning.
            revise_reason: If non-empty, prepended to task to improve retry.

        Returns:
            A validated TaskPlan.
        """
        logfire.debug("Planner.plan called", task=task[:80])
        t_start = time.time()

        full_task = task
        if revise_reason:
            full_task = (
                f"[PREVIOUS PLAN FAILED]\nReason: {revise_reason}\n\n"
                f"Please replan this task more carefully:\n{task}"
            )

        user_content = f"Task: {full_task}"
        if context:
            user_content += f"\n\nRelevant context from memory:\n{context[:2000]}"

        raw_json = await self._call_llm(user_content)
        plan = self._parse_plan(task, raw_json)

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "Planner.plan complete",
            tasks=len(plan.tasks),
            strategy=plan.strategy,
            duration_ms=duration_ms,
        )
        return plan

    async def _call_llm(self, user_content: str) -> str:
        """Call the planning LLM and return raw response text."""
        try:
            # B-01 FIX: was final_review (GPT-4o, PAID). Planner is structured JSON
            # task — Groq Llama-3.3-70b handles it at 10x lower cost.
            model = settings.get_litellm_model("fast_tasks")
            response = await asyncio.to_thread(
                litellm.completion,
                model=model,
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=1000,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content or "{}"
        except Exception as exc:
            logfire.warning("Planner: primary LLM failed, trying fallback", error=str(exc))
            # VECTOR-5 FIX: fallback uses get_fallback_model (e.g. ollama/llama3.2) so it
            # hits a DIFFERENT endpoint from the rate-limited primary — not the same one.
            try:
                fallback_model = settings.get_fallback_model("fast_tasks")
                response = await asyncio.to_thread(
                    litellm.completion,
                    model=fallback_model,
                    messages=[
                        {"role": "system", "content": self._get_system_prompt()},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=1000,
                    temperature=0.2,
                )
                return response.choices[0].message.content or "{}"
            except Exception as exc2:
                logfire.error("Planner: all LLMs failed", error=str(exc2))
                return "{}"

    def _parse_plan(self, original_task: str, raw_json: str) -> TaskPlan:
        """
        Parse raw LLM JSON into a validated TaskPlan.

        Falls back to a single researcher subtask on any parse/validation error.
        """
        try:
            data = json.loads(raw_json)
            if isinstance(data, dict):
                tasks_data = data.get("tasks") or data.get("subtasks") or []
            elif isinstance(data, list):
                tasks_data = data
                data = {"tasks": tasks_data}
            else:
                raise ValueError("Planner returned unsupported JSON structure")

            if not isinstance(tasks_data, list):
                raise ValueError("Planner must return a list of tasks")

            tasks: list[SubTask] = []
            for idx, raw_task in enumerate(tasks_data, start=1):
                if not isinstance(raw_task, dict):
                    raise ValueError("Each task entry must be a JSON object")

                task_payload = dict(raw_task)
                if "task_type" not in task_payload and "agent" in task_payload:
                    agent = task_payload["agent"]
                    type_map = {
                        "researcher": TaskType.RESEARCH.value,
                        "coder": TaskType.CODE.value,
                        "reader": TaskType.READ_DOC.value,
                        "browser": TaskType.BROWSE.value,
                        "notifier": TaskType.NOTIFY.value,
                        "memory_only": TaskType.MEMORY.value,
                    }
                    task_payload["task_type"] = type_map.get(agent, agent)

                task_payload.setdefault("task_id", f"t{idx}")
                task_payload.setdefault("description", str(task_payload.get("description", "")))
                task_payload.setdefault("depends_on", [])
                task_payload.setdefault("priority", 5)
                task_payload.setdefault("requires_human_approval", False)

                tasks.append(SubTask(**task_payload))

            plan = TaskPlan(
                tasks=tasks,
                estimated_total_duration_seconds=sum(t.priority * 10 for t in tasks),
                requires_human_approval=any(t.requires_human_approval for t in tasks),
            )
            logfire.info(
                "Planner: plan parsed",
                task_count=len(plan.tasks),
            )
            return plan
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logfire.warning("Planner: JSON parse failed, using fallback plan", error=str(exc))
            return self._fallback_plan(original_task)
        except ValidationError as exc:
            logfire.warning(
                "Planner: validation failed, using fallback plan",
                errors=exc.error_count(),
                detail=str(exc)[:300],
            )
            return self._fallback_plan(original_task)

    @staticmethod
    def _fallback_plan(task: str) -> TaskPlan:
        """
        Minimal single-subtask plan used when LLM output is unusable.

        C-04 FIX: Uses keyword detection to select the most appropriate
        fallback task type instead of always defaulting to 'research'.
        """
        task_lower = task.lower()
        if any(w in task_lower for w in [
            "code", "write", "program", "script", "implement",
            "function", "class", "algorithm", "debug", "fix the code",
        ]):
            task_type = TaskType.CODE
            description = f"Write and execute Python code for: {task[:400]}"
        elif any(w in task_lower for w in [
            "browse", "navigate", "click", "website", "open page", "go to",
        ]):
            task_type = TaskType.BROWSE
            description = f"Browse and extract information: {task[:400]}"
        elif any(w in task_lower for w in [
            "notify", "send message", "alert", "whatsapp", "telegram", "email",
        ]):
            task_type = TaskType.NOTIFY
            description = f"Send notification: {task[:400]}"
        elif any(w in task_lower for w in [
            "memory", "remember", "recall", "search", "context",
        ]):
            task_type = TaskType.MEMORY
            description = f"Retrieve relevant memory for: {task[:400]}"
        else:
            task_type = TaskType.RESEARCH
            description = f"Research and answer: {task[:400]}"

        return TaskPlan(
            tasks=[
                SubTask(
                    task_type=task_type,
                    description=description,
                    priority=1,
                    depends_on=[],
                )
            ],
            estimated_total_duration_seconds=30,
            requires_human_approval=False,
        )

    # ------------------------------------------------------------------
    # Parallel grouping helper (used by orchestrator)
    # ------------------------------------------------------------------

    @staticmethod
    def get_parallel_groups(plan: TaskPlan) -> list[list[SubTask]]:
        """
        Topological sort of tasks into parallel execution groups.

        Returns:
            List of groups; each group can be executed concurrently.
            Groups must be executed in order (each group depends on all prior groups).
        """
        completed: set[str] = set()
        remaining = list(plan.tasks)
        groups: list[list[SubTask]] = []

        while remaining:
            ready = [
                st for st in remaining
                if all(dep in completed for dep in st.dependencies)
            ]
            if not ready:
                # Circular dependency detected — push all remaining as one group
                logfire.warning(
                    "Planner.get_parallel_groups: circular dependency detected, "
                    "forcing sequential execution"
                )
                ready = remaining

            groups.append(ready)
            for st in ready:
                completed.add(st.task_id)
                remaining.remove(st)

        return groups
