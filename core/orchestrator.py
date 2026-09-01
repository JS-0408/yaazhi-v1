"""
Yaazhi Orchestrator — the LangGraph master control loop.

Routes every user request through a plan → execute → review → finalize
pipeline using a stateful LangGraph graph. Supports parallel subtask
execution via asyncio.gather and enforces a maximum loop count to
prevent infinite reasoning cycles.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any, Optional

import logfire
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from agents.browser import BrowserAgent
from agents.coder import CoderAgent
from agents.notifier import NotifierAgent
from agents.reader import ReaderAgent
from agents.researcher import ResearcherAgent
from config.settings import settings
from core.guardrails import validate_user_input
from core.planner import Planner
from core.reviewer import Reviewer

# Backwards-compatible alias for test patch targets expecting ReviewerAgent
ReviewerAgent = Reviewer
from core.state import (
    AgentOutput,
    ReviewVerdict,
    SubTask,
    TaskType,
    YaazhiOutput,
    YaazhiState,
    make_initial_state,
)
from memory.retriever import SemanticRetriever


# ─── Orchestrator ─────────────────────────────────────────────────────────────

class Yaazhi:
    """
    The master LangGraph orchestrator for the Yaazhi AI system.

    Builds a StateGraph with nodes for planning, routing, executing each
    agent type, reviewing outputs, and finalizing responses. Supports
    parallel subtask execution and enforces a configurable loop limit.

    Attributes:
        planner: Planner instance for task decomposition.
        reviewer: Reviewer instance for output quality gating.
        researcher: ResearcherAgent instance.
        coder: CoderAgent instance.
        reader: ReaderAgent instance.
        browser: BrowserAgent instance.
        notifier: NotifierAgent instance.
        retriever: SemanticRetriever for memory context.
        graph: Compiled LangGraph StateGraph.
        _max_loops: Maximum reviewer loop iterations.
    """

    def __init__(self) -> None:
        """
        Initialise all agents, memory, and build the LangGraph.

        Raises:
            RuntimeError: If any critical service fails its ping check.
        """
        logfire.info("Yaazhi orchestrator initialising")
        self.planner = Planner()
        self.reviewer = Reviewer()
        self.researcher = ResearcherAgent()
        self.coder = CoderAgent()
        self.reader = ReaderAgent()
        self.browser = BrowserAgent()
        self.notifier = NotifierAgent()
        self.retriever = SemanticRetriever()
        self._max_loops: int = settings.max_loop_count
        self.graph = self._build_graph()
        logfire.info("Yaazhi orchestrator ready", max_loops=self._max_loops)

    def __repr__(self) -> str:
        """Return string representation."""
        return f"Yaazhi(max_loops={self._max_loops}, agents=7)"

    async def ping(self) -> bool:
        """
        Health check — verify all core components respond.

        Returns:
            True if planner and reviewer both respond, False otherwise.
        """
        try:
            planner_ok, reviewer_ok = await asyncio.gather(
                self.planner.ping(),
                self.reviewer.ping(),
                return_exceptions=True,
            )
            return bool(planner_ok) and bool(reviewer_ok)
        except Exception as exc:
            logfire.error("Yaazhi ping failed", error=str(exc))
            return False

    # ─── Graph Node Implementations ───────────────────────────────────────────

    async def _planner_node(self, state: YaazhiState) -> YaazhiState:
        """
        Plan node: decompose user_input into a TaskPlan.

        Args:
            state: Current graph state.

        Returns:
            Updated state with current_tasks populated.
        """
        logfire.debug("Planner node executing")
        try:
            plan = await self.planner.plan(
                user_input=state["user_input"],
                context=state.get("memory_context", ""),
            )
            state["current_tasks"] = [t.model_dump() for t in plan.tasks]
            logfire.info("Planner node complete", task_count=len(plan.tasks))
        except Exception as exc:
            logfire.error("Planner node failed", error=str(exc))
            state["error_log"].append(f"Planner error: {exc}")
            # Fallback: create a single research task
            fallback_task = SubTask(
                task_type=TaskType.RESEARCH,
                description=state["user_input"],
                priority=1,
            )
            state["current_tasks"] = [fallback_task.model_dump()]
        return state

    async def _execute_single_task(self, task_dict: dict[str, Any], state: YaazhiState) -> AgentOutput:
        """
        Execute a single SubTask using the appropriate agent.

        Args:
            task_dict: SubTask serialised as dict.
            state: Current graph state for context.

        Returns:
            AgentOutput from the executed agent.
        """
        task = SubTask(**task_dict)
        start = time.perf_counter()
        content: str = ""
        model_used: str = ""
        success: bool = True
        error_msg: Optional[str] = None

        try:
            if task.task_type == TaskType.RESEARCH:
                result = await self.researcher.research(
                    topic=task.description, depth="quick"
                )
                content = result.summary
                model_used = settings.get_litellm_model("research")

            elif task.task_type == TaskType.CODE:
                result = await self.coder.code_loop(
                    task_description=task.description,
                    context=state.get("memory_context", ""),
                )
                content = f"```python\n{result.code}\n```\n\nOutput:\n{result.output}"
                model_used = settings.get_litellm_model("code")

            elif task.task_type == TaskType.READ_DOC:
                # Extract URL or file path from description
                import re
                url_match = re.search(r"https?://\S+", task.description)
                if url_match:
                    result = await self.reader.read_url(url_match.group(0))
                else:
                    result = await self.reader.summarize(task.description, style="academic")
                    content = result
                    model_used = settings.get_litellm_model("read_doc")
                if hasattr(result, "summary"):
                    content = result.summary  # type: ignore[union-attr]
                model_used = settings.get_litellm_model("read_doc")

            elif task.task_type == TaskType.BROWSE:
                result = await self.browser.search_web(task.description)
                snippets = [r.get("snippet", "") for r in result[:3]]
                content = "\n\n".join(snippets) or "No results found"
                model_used = "playwright"

            elif task.task_type == TaskType.NOTIFY:
                await self.notifier.task_complete_alert(
                    task_name=task.description,
                    result_summary="Task queued for notification",
                    duration_seconds=0.0,
                )
                content = "Notification sent successfully"
                model_used = "n8n"

            elif task.task_type == TaskType.MEMORY:
                result = await self.retriever.retrieve(query=task.description, top_k=5)
                content = "\n".join([r.text for r in result]) or "No memories found"
                model_used = "chromadb"

            elif task.task_type == TaskType.PRIVATE:
                # Route to local Ollama — never leaves VPS
                import litellm
                response = await asyncio.to_thread(
                    litellm.completion,
                    model=settings.get_litellm_model("private"),
                    messages=[{"role": "user", "content": task.description}],
                    max_tokens=2000,
                    timeout=120,
                )
                content = response.choices[0].message.content or ""
                model_used = settings.get_litellm_model("private")

            else:
                content = f"Task type {task.task_type.value} not yet implemented"
                success = False

        except Exception as exc:
            logfire.error(
                "Task execution failed",
                task_id=task.task_id[:8],
                task_type=task.task_type.value,
                error=str(exc),
            )
            content = f"Error executing task: {exc}"
            success = False
            error_msg = str(exc)

        duration_ms = int((time.perf_counter() - start) * 1000)
        return AgentOutput(
            agent_name=task.task_type.value,
            task_id=task.task_id,
            content=content,
            success=success,
            error_message=error_msg,
            model_used=model_used,
            duration_ms=duration_ms,
        )

    async def _executor_node(self, state: YaazhiState) -> YaazhiState:
        """
        Executor node: run all pending tasks, parallel where possible.

        Args:
            state: Current graph state.

        Returns:
            Updated state with agent_outputs populated.
        """
        logfire.debug("Executor node starting", task_count=len(state["current_tasks"]))
        from core.planner import Planner

        # Identify which tasks haven't been completed yet
        completed_ids: set[str] = {t["task_id"] for t in state.get("completed_tasks", [])}
        pending = [t for t in state["current_tasks"] if t["task_id"] not in completed_ids]

        # Build parallel execution groups using dependency analysis
        # VECTOR-4 FIX: reuse self.planner — was creating a new Planner() on every
        # executor node execution (15 redundant inits in a 5-loop, 3-task run).
        from core.state import SubTask as ST, TaskPlan
        try:
            tasks_obj = [ST(**t) for t in pending]
            fake_plan = TaskPlan(tasks=tasks_obj if tasks_obj else [ST(task_type=TaskType.RESEARCH, description="placeholder")])
            groups = self.planner.get_parallel_groups(fake_plan)
        except Exception:
            # Fallback: execute sequentially
            groups = [[t] for t in pending]  # type: ignore[assignment]

        for group in groups:
            group_dicts = [t.model_dump() if hasattr(t, "model_dump") else t for t in group]
            results = await asyncio.gather(
                *[self._execute_single_task(td, state) for td in group_dicts],
                return_exceptions=True,
            )
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logfire.error("Task group execution error", error=str(result))
                    task_id = group_dicts[i].get("task_id", str(uuid.uuid4()))
                    output = AgentOutput(
                        agent_name="executor",
                        task_id=task_id,
                        content=f"Execution error: {result}",
                        success=False,
                        error_message=str(result),
                    )
                    state["agent_outputs"][task_id] = output.model_dump()
                else:
                    typed_result: AgentOutput = result  # type: ignore[assignment]
                    state["agent_outputs"][typed_result.task_id] = typed_result.model_dump()
                    state["completed_tasks"].append(group_dicts[i])

        logfire.info("Executor node complete", outputs=len(state["agent_outputs"]))
        return state

    async def _reviewer_node(self, state: YaazhiState) -> YaazhiState:
        """
        Reviewer node: quality-gate all agent outputs.

        Args:
            state: Current graph state.

        Returns:
            Updated state with review results in metadata.
        """
        logfire.debug("Reviewer node starting")
        state["loop_count"] = state.get("loop_count", 0) + 1

        all_passed = True
        combined_feedback: list[str] = []
        combined_context: list[str] = []

        for task_dict in state.get("current_tasks", []):
            task_id = task_dict.get("task_id", "")
            output_dict = state["agent_outputs"].get(task_id)
            if not output_dict:
                continue

            output = AgentOutput(**output_dict)
            task = SubTask(**task_dict)

            try:
                result = await self.reviewer.review(output, task)
                if result.verdict == ReviewVerdict.FAIL:
                    all_passed = False
                    combined_feedback.append(f"Task {task_id[:8]}: FAIL — {result.feedback}")
                    # Remove this task from completed so it can be re-executed
                    state["completed_tasks"] = [
                        t for t in state["completed_tasks"] if t.get("task_id") != task_id
                    ]
                elif result.verdict == ReviewVerdict.REVISE:
                    all_passed = False
                    combined_feedback.append(f"Task {task_id[:8]}: REVISE — {result.feedback}")
                    combined_context.append(result.retry_with_context)
                    state["completed_tasks"] = [
                        t for t in state["completed_tasks"] if t.get("task_id") != task_id
                    ]
            except Exception as exc:
                logfire.error("Review of task failed", task_id=task_id[:8], error=str(exc))
                # On review error, assume PASS to avoid infinite loop
                combined_feedback.append(f"Review error on {task_id[:8]}: {exc}")

        state["metadata"]["review_feedback"] = "\n".join(combined_feedback)
        state["metadata"]["retry_context"] = "\n".join(combined_context)
        state["metadata"]["all_reviews_passed"] = all_passed

        logfire.info(
            "Reviewer node complete",
            loop=state["loop_count"],
            all_passed=all_passed,
        )
        return state

    async def _finalizer_node(self, state: YaazhiState) -> YaazhiState:
        """
        Finalizer node: synthesize all agent outputs into one answer.

        Args:
            state: Current graph state.

        Returns:
            Updated state with final_output populated.
        """
        logfire.debug("Finalizer node executing")
        # Combine all agent outputs into a coherent response
        output_texts = [
            v["content"] for v in state["agent_outputs"].values()
            if v.get("success") and v.get("content")
        ]

        if not output_texts:
            final_text = "I was unable to generate a response for your request. Please try again."
        elif len(output_texts) == 1:
            final_text = output_texts[0]
        else:
            # Synthesize multiple outputs
            import litellm
            synthesis_prompt = (
                f"Synthesize these agent results into one clear, helpful response "
                f"for the user's original request: '{state['user_input']}'\n\n"
                + "\n\n---\n\n".join(output_texts[:5])
            )
            try:
                # B-02 FIX: acompletion is fully async — no thread blocking
                response = await litellm.acompletion(
                    model=settings.get_litellm_model("review"),
                    messages=[{"role": "user", "content": synthesis_prompt}],
                    max_tokens=2000,
                    timeout=60,
                )
                final_text = response.choices[0].message.content or "\n\n".join(output_texts)
            except Exception as exc:
                logfire.warning("Synthesis failed, concatenating", error=str(exc))
                final_text = "\n\n".join(output_texts)

        started_at = state["metadata"].get("started_at", datetime.utcnow().isoformat())
        try:
            started_dt = datetime.fromisoformat(started_at)
            total_ms = int((datetime.utcnow() - started_dt).total_seconds() * 1000)
        except ValueError:
            total_ms = 0

        agents_called = list({v["agent_name"] for v in state["agent_outputs"].values()})
        memories_used = len(state.get("memory_context", "").split("\n"))

        output = YaazhiOutput(
            response=final_text,
            session_id=state["session_id"],
            confidence_score=0.85,
            sources_used=[],
            agents_called=agents_called,
            warnings=[],
            processing_time_ms=total_ms,
            model_used=settings.get_litellm_model("review"),
            detected_language=state.get("detected_language", "en"),
            memories_used=memories_used,
        )
        state["final_output"] = output.model_dump()
        logfire.info("Finalizer node complete", response_length=len(final_text), total_ms=total_ms)
        return state

    # ─── Graph Edge Conditions ────────────────────────────────────────────────

    def _should_loop_or_end(self, state: YaazhiState) -> str:
        """
        Conditional edge: decide whether to loop back or finalize.

        Args:
            state: Current graph state.

        Returns:
            'loop' to re-execute failed tasks, 'finalize' to end.
        """
        all_passed = state["metadata"].get("all_reviews_passed", True)
        loop_count = state.get("loop_count", 0)
        max_loops = state.get("max_loops", self._max_loops)

        if all_passed or loop_count >= max_loops:
            if loop_count >= max_loops:
                logfire.warning("Max loops reached, forcing finalization", loop_count=loop_count)
                state["metadata"].setdefault("warnings", []).append(
                    f"Response quality check reached max iterations ({max_loops})"
                )
            return "finalize"
        return "loop"

    # ─── Graph Construction ───────────────────────────────────────────────────

    def _build_graph(self) -> Any:
        """
        Build and compile the LangGraph StateGraph.

        Returns:
            Compiled LangGraph runnable.
        """
        graph = StateGraph(YaazhiState)

        graph.add_node("planner", self._planner_node)
        graph.add_node("executor", self._executor_node)
        graph.add_node("reviewer", self._reviewer_node)
        graph.add_node("finalizer", self._finalizer_node)

        graph.set_entry_point("planner")
        graph.add_edge("planner", "executor")
        graph.add_edge("executor", "reviewer")
        graph.add_conditional_edges(
            "reviewer",
            self._should_loop_or_end,
            {"loop": "executor", "finalize": "finalizer"},
        )
        graph.add_edge("finalizer", END)

        return graph.compile()

    # ─── Public API ──────────────────────────────────────────────────────────

    async def run(self, user_input: str, session_id: Optional[str] = None) -> YaazhiOutput:
        """
        Process a user request end-to-end through the LangGraph pipeline.

        Args:
            user_input: The raw user message.
            session_id: Optional conversation session ID. Generated if not provided.

        Returns:
            YaazhiOutput with the final synthesized response.

        Raises:
            ValueError: If input fails guardrail validation.
        """
        start_time = time.perf_counter()
        if not session_id:
            session_id = str(uuid.uuid4())

        # Input validation via guardrails
        validated = validate_user_input(user_input)
        logfire.info(
            "Yaazhi.run starting",
            session_id=session_id[:8],
            language=validated.detected_language.value,
            input_length=validated.char_count,
        )

        # Retrieve memory context
        try:
            memory_context = await self.retriever.build_context(
                query=validated.sanitized_text, max_tokens=1500
            )
        except Exception as exc:
            logfire.warning("Memory retrieval failed, continuing without context", error=str(exc))
            memory_context = ""

        # Build initial state
        initial_state = make_initial_state(
            user_input=validated.sanitized_text,
            session_id=session_id,
            max_loops=self._max_loops,
        )
        initial_state["detected_language"] = validated.detected_language.value
        initial_state["memory_context"] = memory_context

        # Execute the LangGraph pipeline
        config: RunnableConfig = {"configurable": {"session_id": session_id}}
        try:
            final_state: YaazhiState = await self.graph.ainvoke(initial_state, config=config)
        except Exception as exc:
            logfire.error("LangGraph pipeline failed", session_id=session_id[:8], error=str(exc))
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            return YaazhiOutput(
                response=f"I encountered an error processing your request: {exc}",
                session_id=session_id,
                confidence_score=0.0,
                agents_called=[],
                warnings=[str(exc)],
                processing_time_ms=duration_ms,
                model_used="",
                detected_language=validated.detected_language.value,
                memories_used=0,
            )

        output_dict = final_state.get("final_output", {})
        if output_dict:
            return YaazhiOutput(**output_dict)

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        return YaazhiOutput(
            response="Processing complete but no output was generated.",
            session_id=session_id,
            confidence_score=0.5,
            agents_called=[],
            warnings=["No output from pipeline"],
            processing_time_ms=duration_ms,
            model_used="",
            detected_language=validated.detected_language.value,
            memories_used=0,
        )
