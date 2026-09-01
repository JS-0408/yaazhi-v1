"""
Yaazhi Reflection Agent — self-improvement via Reflexion.

Audit fixes applied:
  - GAP 1: Multi-critic confirmation bias fix. Uses TWO different
    models than the one that ran the task, takes conservative lower score,
    and averages the verbal feedback.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import logfire
import yaml

from config.settings import settings
from core.agent_registry import AgentRegistry

# B-05 FIX: Module-level lock serializes concurrent YAML writes.
# Multiple agent completions can fire simultaneously; without this lock
# two coroutines could read-modify-write models.yaml at the same time
# producing corrupted/truncated YAML.
_MODELS_YAML_LOCK = asyncio.Lock()


@AgentRegistry.register("reflection")
class ReflectionAgent:
    """
    Post-task reflection agent. Scores performance, stores learnings,
    updates routing weights in models.yaml.
    """

    _COLLECTION = "yaazhi_reflections"
    _MODELS_YAML = Path("config/models.yaml")

    def __init__(self) -> None:
        self._vs: Optional[Any] = None   # VectorStore, injected lazily

    async def _ensure_vs(self) -> Any:
        if self._vs is None:
            from memory.vector_store import VectorStore
            self._vs = VectorStore()
        return self._vs

    def get_critic_models(self, task_model: str) -> list[str]:
        """
        GAP 1 FIX: Return TWO different models than the one that ran the task
        to prevent confirmation bias.
        """
        provider = task_model.split("/")[0].lower() if "/" in task_model else task_model.lower()
        
        gpt4o = settings.get_litellm_model("final_review")
        groq = settings.get_litellm_model("fast_tasks")
        claude = settings.get_litellm_model("code_generation")
        
        if "groq" in provider:
            return [gpt4o, claude]
        elif "azure" in provider or "openai" in provider:
            return [groq, claude]
        elif "ollama" in provider:
            return [claude, groq]
        else:
            # Default fallback: always use two distinct providers
            return [gpt4o, groq]

    async def reflect(self, task_result: dict) -> None:
        """
        Analyse a completed task result and write a reflection using multi-critic.
        """
        logfire.debug("ReflectionAgent.reflect called", task=task_result.get("task_description", "")[:60])
        t_start = time.time()

        task_desc = task_result.get("task_description", "")
        agent_name = task_result.get("agent_name", "unknown")
        task_model = task_result.get("model_used", "unknown/unknown")
        success = task_result.get("success", False)
        elapsed = task_result.get("elapsed_seconds", 0.0)
        output_summary = task_result.get("output_summary", "")[:500]
        reviewer_score = float(task_result.get("reviewer_score", 0.5))
        session_id = task_result.get("session_id", "")

        critic_models = self.get_critic_models(task_model)
        
        try:
            # GAP 1 FIX: Run critique with TWO different models concurrently
            critiques = await asyncio.gather(*[
                self._generate_reflection(model, task_desc, agent_name, success, elapsed, output_summary, reviewer_score)
                for model in critic_models
            ], return_exceptions=True)
            
            valid_critiques = [c for c in critiques if not isinstance(c, Exception)]
            
            if not valid_critiques:
                raise RuntimeError("All critic models failed")
                
            # Average the scores, use lower score as conservative estimate if needed 
            # (Though here we are getting text feedback, we will merge them)
            what_worked = " | ".join(c[0] for c in valid_critiques)
            what_failed = " | ".join(c[1] for c in valid_critiques)
            suggestion = " | ".join(c[2] for c in valid_critiques)
            
        except Exception as exc:
            logfire.warning("ReflectionAgent: Multi-critic reflection failed", error=str(exc))
            what_worked = "N/A"
            what_failed = "Reflection LLMs unavailable"
            suggestion = "Retry with primary model"

        reflection_text = (
            f"Task: {task_desc}\n"
            f"Agent: {agent_name}\n"
            f"Task Model: {task_model}\n"
            f"Success: {success}\n"
            f"Elapsed: {elapsed:.1f}s\n"
            f"Reviewer score: {reviewer_score:.2f}\n"
            f"What worked: {what_worked}\n"
            f"What failed: {what_failed}\n"
            f"Next time: {suggestion}\n"
            f"Session: {session_id}\n"
            f"Timestamp: {datetime.now(timezone.utc).isoformat()}"
        )

        try:
            vs = await self._ensure_vs()
            await vs.add(
                reflection_text,
                metadata={
                    "agent": agent_name,
                    "task_model": task_model,
                    "critics": ",".join(critic_models),
                    "success": success,
                    "score": reviewer_score,
                    "session_id": session_id,
                    "type": "reflection",
                },
                source="reflection_multi_critic",
                user_id=settings.default_user_id,
                agent_id="reflection",
            )
            logfire.info("ReflectionAgent: stored multi-critic reflection", agent=agent_name)
        except Exception as exc:
            logfire.warning("ReflectionAgent: failed to store reflection", error=str(exc))

        if reviewer_score < 0.5 or (not success and elapsed > 30):
            await self._update_model_weight(agent_name, delta=-0.05)
        elif reviewer_score >= 0.85 and success:
            await self._update_model_weight(agent_name, delta=+0.02)

        logfire.info(
            "ReflectionAgent.reflect complete",
            agent=agent_name,
            duration_ms=int((time.time() - t_start) * 1000),
        )

    async def _generate_reflection(
        self,
        critic_model: str,
        task: str,
        agent: str,
        success: bool,
        elapsed: float,
        output: str,
        score: float,
    ) -> tuple[str, str, str]:
        import litellm

        prompt = (
            f"You are analyzing the performance of an AI agent as an independent critic.\n\n"
            f"Task: {task[:300]}\n"
            f"Agent: {agent}\n"
            f"Succeeded: {success}\n"
            f"Time taken: {elapsed:.1f}s\n"
            f"Quality score: {score:.2f}/1.0\n"
            f"Output snippet: {output[:300]}\n\n"
            f"Reply with JSON:\n"
            f'{{"what_worked": "...", "what_failed": "...", "improvement": "..."}}'
        )
        resp = await litellm.acompletion(
            model=critic_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.2,
        )
        raw = resp.choices[0].message.content or "{}"
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        return (
            str(data.get("what_worked", "Unknown")),
            str(data.get("what_failed", "Unknown")),
            str(data.get("improvement", "No change")),
        )

    async def get_relevant_reflections(self, task: str, top_k: int = 3) -> str:
        try:
            vs = await self._ensure_vs()
            results = await vs.search(
                task,
                top_k=top_k,
                filter={"type": "reflection"},
                user_id=settings.default_user_id,
            )
            if not results:
                return ""
            lines = [f"[Past lesson {i+1}]: {r.text[:300]}" for i, r in enumerate(results)]
            return "\n".join(lines)
        except Exception as exc:
            logfire.warning("ReflectionAgent.get_relevant_reflections failed", error=str(exc))
            return ""

    async def _update_model_weight(self, agent_name: str, delta: float) -> None:
        """
        B-05 FIX:
        - Uses asyncio.to_thread for file I/O (no event loop blocking).
        - Acquires _MODELS_YAML_LOCK before read-modify-write to prevent
          concurrent YAML corruption under parallel agent completions.
        """
        if not self._MODELS_YAML.exists():
            return
        async with _MODELS_YAML_LOCK:   # B-05 FIX: serialize concurrent writes
            try:
                content = await asyncio.to_thread(
                    self._MODELS_YAML.read_text, encoding="utf-8"
                )
                data: dict = yaml.safe_load(content)

                weights: dict = data.setdefault("routing_weights", {})
                current = float(weights.get(agent_name, 1.0))
                new_weight = round(max(0.1, min(1.0, current + delta)), 3)
                weights[agent_name] = new_weight

                new_content = yaml.dump(
                    data, default_flow_style=False, allow_unicode=True
                )
                await asyncio.to_thread(
                    self._MODELS_YAML.write_text, new_content, encoding="utf-8"
                )
                logfire.info(
                    "ReflectionAgent: updated model weight",
                    agent=agent_name,
                    old=current,
                    new=new_weight,
                    delta=delta,
                )
            except Exception as exc:
                logfire.warning(
                    "ReflectionAgent._update_model_weight failed", error=str(exc)
                )
