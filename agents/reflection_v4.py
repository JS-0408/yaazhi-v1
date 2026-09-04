"""
Yaazhi V4 — Asynchronous Reflection Pipeline (Step 5).

Runs as a background Celery-compatible async task after each orchestrator
cycle.  Does NOT block the user-facing response.

Responsibilities:
  1. Episodic Logging   — write raw execution telemetry to `episodic_events`.
  2. Fact Abstraction   — (Layer 9) convert repetitive events into semantic
                          :Fact nodes in Neo4j (confidence ≥ 0.85 threshold).
  3. Graph Mutation     — update Neo4j edge weights; mark outdated nodes
                          SUPERSEDED (Layer 8 Graph Sync).
  4. Affective Update   — (Layer 7) measure command frequency + error density
                          → update burnout_score in `akam_user_state`.

Usage (inside orchestrator, post-response)::

    asyncio.create_task(
        reflection_pipeline.run(
            session_outputs=state["agent_outputs"],
            tinai=route.tinai,
            kalam_epoch=route.kalam_epoch,
            user_id=session_id,
        )
    )
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any, Optional

import logfire

from memory.akam_store import akam_store
from memory.event_logger import event_logger
from memory.graph_store import graph_store, RelType


# ─── Fact Abstraction Threshold ──────────────────────────────────────────────

_FACT_CONFIDENCE_THRESHOLD = 0.85   # V4 spec: Confidence ≥ 0.85
_MIN_EVENT_RECURRENCE      = 3      # Minimum identical event_type count to abstract


# ─── Reflection Pipeline ─────────────────────────────────────────────────────

class ReflectionPipeline:
    """
    Asynchronous post-execution reflection engine.

    All methods are designed to be fire-and-forget (wrapped in asyncio.create_task)
    and must never raise exceptions that propagate to the caller.
    """

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        session_outputs: dict[str, dict[str, Any]],
        tinai:           str = "general_technical",
        kalam_epoch:     str = "University_Builder_Epoch",
        user_id:         Optional[str] = None,
        session_id:      Optional[str] = None,
    ) -> None:
        """
        Execute the full reflection cycle asynchronously.

        Args:
            session_outputs: Dict of task_id → AgentOutput.model_dump() from
                             the completed orchestrator cycle.
            tinai:           Active Tiṇai for this cycle.
            kalam_epoch:     Active Kālam epoch.
            user_id:         User namespace for Akam updates.
            session_id:      Session identifier for event correlation.
        """
        logfire.info(
            "ReflectionPipeline.run: starting",
            tasks=len(session_outputs),
            tinai=tinai,
        )
        # Run all reflection steps concurrently — failures are isolated.
        results = await asyncio.gather(
            self._step1_log_events(session_outputs, tinai, kalam_epoch, session_id),
            self._step4_affective_update(session_outputs, user_id),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logfire.error("ReflectionPipeline step failed", error=str(r))

        # Step 2 & 3 require the events to be written first (fetch from PG)
        await self._step2_fact_abstraction(tinai, kalam_epoch)

        logfire.info("ReflectionPipeline.run: complete")

    # ------------------------------------------------------------------
    # Step 1 — Episodic Logging
    # ------------------------------------------------------------------

    async def _step1_log_events(
        self,
        session_outputs: dict[str, dict[str, Any]],
        tinai:           str,
        kalam_epoch:     str,
        session_id:      Optional[str],
    ) -> None:
        """Write each AgentOutput as a row in `episodic_events`."""
        tasks = []
        for task_id, output_dict in session_outputs.items():
            event_type = output_dict.get("agent_name", "unknown")
            summary    = output_dict.get("content", "")[:500]
            payload    = {
                "task_id":     task_id,
                "success":     output_dict.get("success", False),
                "duration_ms": output_dict.get("duration_ms", 0),
                "model_used":  output_dict.get("model_used", ""),
                "session_id":  session_id or "",
            }
            tasks.append(
                event_logger.log(
                    tinai           = tinai,
                    kalam_epoch     = kalam_epoch,
                    orchestrator_id = event_type,
                    event_type      = event_type,
                    summary         = summary,
                    context_payload = payload,
                )
            )
        await asyncio.gather(*tasks, return_exceptions=True)
        logfire.debug("ReflectionPipeline: step1 logged events", count=len(tasks))

    # ------------------------------------------------------------------
    # Step 2 — Fact Abstraction (Layer 9)
    # ------------------------------------------------------------------

    async def _step2_fact_abstraction(
        self,
        tinai:       str,
        kalam_epoch: str,
    ) -> None:
        """
        Scan recent episodic events; if any event_type recurs ≥ threshold,
        abstract it into a :Fact node in Neo4j (confidence ≥ 0.85).
        """
        try:
            recent = await event_logger.fetch_recent(tinai=tinai, limit=100)
        except Exception as exc:
            logfire.warning("ReflectionPipeline: step2 fetch failed", error=str(exc))
            return

        if not recent:
            return

        # Count event_type recurrences
        type_counter: Counter[str] = Counter(e.get("event_type", "") for e in recent)

        for event_type, count in type_counter.items():
            if count < _MIN_EVENT_RECURRENCE:
                continue

            # Sample most-recent summary as the candidate fact text
            exemplars = [e for e in recent if e.get("event_type") == event_type]
            if not exemplars:
                continue
            sample_text = exemplars[0].get("summary", "").strip()
            if len(sample_text) < 10:
                continue

            fact_text  = f"Recurring pattern [{event_type}] in [{tinai}]: {sample_text}"
            confidence = min(0.95, _FACT_CONFIDENCE_THRESHOLD + (count * 0.01))

            new_id = await graph_store.add_fact(
                text       = fact_text,
                tinai      = tinai,
                confidence = confidence,
                source     = "reflection_pipeline",
            )

            if new_id:
                logfire.info(
                    "ReflectionPipeline: fact abstracted",
                    event_type = event_type,
                    count      = count,
                    fact_id    = new_id[:8] if new_id else "?",
                    confidence = round(confidence, 3),
                )

                # Step 3 — reinforce BELONGS_TO_TINAI edge weight
                try:
                    # Fetch the context node for this Tiṇai
                    await graph_store.update_edge_weight(
                        from_id  = new_id,
                        to_id    = tinai,    # Context node keyed by Tiṇai name
                        rel_type = RelType.BELONGS_TO_TINAI,
                        delta    = 0.1 * count,
                    )
                except Exception as exc:
                    logfire.warning(
                        "ReflectionPipeline: edge weight update failed",
                        error=str(exc),
                    )

    # ------------------------------------------------------------------
    # Step 4 — Affective Update (Layer 7)
    # ------------------------------------------------------------------

    async def _step4_affective_update(
        self,
        session_outputs: dict[str, dict[str, Any]],
        user_id:         Optional[str],
    ) -> None:
        """
        Update burnout_score from the session's command/error density.
        """
        command_count = len(session_outputs)
        error_count   = sum(
            1 for o in session_outputs.values()
            if not o.get("success", True)
        )
        try:
            new_score = await akam_store.update_burnout(
                command_count = command_count,
                error_count   = error_count,
                user_id       = user_id,
            )
            logfire.debug(
                "ReflectionPipeline: burnout updated",
                new_score=round(new_score, 3),
                commands=command_count,
                errors=error_count,
            )
        except Exception as exc:
            logfire.warning("ReflectionPipeline: step4 failed", error=str(exc))


# ─── Module-level singleton ───────────────────────────────────────────────────
reflection_pipeline = ReflectionPipeline()
