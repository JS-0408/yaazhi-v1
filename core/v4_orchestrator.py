"""
Yaazhi V4 Orchestrator — Full architecture integration.

Wires the 5-step V4 execution workflow into the existing LangGraph pipeline:

  Step 1: Query Ingestion & Mutarporuḷ Extraction  → CognitiveRouter
  Step 2: Domain Routing (Akam vs. Puram)          → AkamStore / SemanticRetriever
  Step 3: Context Assembly (2,048-token budget)    → ContextAssembler
  Step 4: Multi-Orchestrator Execution             → Uriporuḷ SM + Agent Worker Pool
  Step 5: Asynchronous Reflection                  → ReflectionPipeline (background task)

This class extends (wraps) the existing Yaazhi orchestrator to preserve
full backwards-compatibility with the API layer (api/main.py).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Optional

import logfire

from agents.reflection_v4 import reflection_pipeline
from config.settings import settings
from core.context_assembler import context_assembler
from core.orchestrator import Yaazhi
from core.router import CognitiveRouter, Domain, KalamEpoch, RouteDecision
from core.state import SubTask, YaazhiOutput, YaazhiState, make_initial_state
from core.uriporul import UriporuḷStateMachine, uriporul_sm
from memory.akam_store import akam_store
from memory.retriever import SemanticRetriever


# ─── V4 Orchestrator ─────────────────────────────────────────────────────────

class YaazhiV4(Yaazhi):
    """
    Yaazhi V4 — 12-layer cognitive architecture with Akam/Puram isolation.

    Inherits all agents, memory, and LangGraph infrastructure from the base
    Yaazhi class.  Overrides `run()` to insert the V4 routing and context
    assembly steps before handing off to the existing agent execution graph.

    New attributes:
        router       — CognitiveRouter for domain + Tiṇai classification.
        uriporul_sm  — UriporuḷStateMachine for phase assignment.
        v4_retriever — SemanticRetriever used for Tiṇai-filtered context.
    """

    def __init__(self) -> None:
        super().__init__()
        self.router      = CognitiveRouter()
        self.uriporul_sm = uriporul_sm
        self.v4_retriever = SemanticRetriever()
        logfire.info("YaazhiV4 orchestrator ready")

    def __repr__(self) -> str:
        return f"YaazhiV4(max_loops={self._max_loops}, layers=12)"

    # ------------------------------------------------------------------
    # Public API — V4 run() overrides base run()
    # ------------------------------------------------------------------

    async def run(  # type: ignore[override]
        self,
        user_input:   str,
        session_id:   Optional[str]   = None,
        kalam_epoch:  Optional[KalamEpoch] = None,
    ) -> YaazhiOutput:
        """
        Process a user request through the full V4 5-step pipeline.

        Args:
            user_input:  Raw user message.
            session_id:  Optional session ID; generated if absent.
            kalam_epoch: Override the active temporal epoch.

        Returns:
            YaazhiOutput with final synthesized response.
        """
        start_time = time.perf_counter()
        session_id = session_id or str(uuid.uuid4())

        # ── Step 1: Mutarporuḷ Extraction ────────────────────────────
        route = self.router.classify(user_input, kalam_epoch=kalam_epoch)
        logfire.info(
            "V4.run: Step1 complete",
            domain=route.domain,
            tinai=route.tinai,
            confidence=route.confidence,
        )

        # ── Step 2: Domain Routing ────────────────────────────────────
        akam_context  = ""
        puram_context = ""

        if route.domain == Domain.AKAM:
            akam_state = await akam_store.load()
            akam_context = akam_state.to_context_block()
            logfire.info("V4.run: Step2 → Akam route", burnout=akam_state.burnout_score)
        else:
            # Puram: retrieve vector context restricted to this Tiṇai
            try:
                raw_results = await self.v4_retriever.retrieve(user_input, top_k=8)
                vector_dicts = [
                    {
                        "content":    r.text,
                        "score":      r.score,
                        "source":     r.source,
                        "tinai":      r.metadata.get("tinai", route.tinai),
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in raw_results
                ]
            except Exception as exc:
                logfire.warning("V4.run: vector retrieval failed", error=str(exc))
                vector_dicts = []
            logfire.info("V4.run: Step2 → Puram route", vectors=len(vector_dicts))

        # ── Step 3: Context Assembly ──────────────────────────────────
        try:
            assembled = await context_assembler.assemble(
                query          = user_input,
                route          = route,
                vector_results = vector_dicts if route.domain == Domain.PURAM else [],
            )
        except Exception as exc:
            logfire.warning("V4.run: context assembly failed", error=str(exc))
            assembled = akam_context  # fallback to Akam snapshot if Puram assembly fails

        combined_context = "\n".join(filter(None, [akam_context, assembled]))

        # ── Step 4: Multi-Orchestrator Execution via base graph ───────
        initial_state: YaazhiState = make_initial_state(
            user_input = user_input,
            session_id = session_id,
            max_loops  = self._max_loops,
        )
        initial_state["memory_context"] = combined_context[:3000]
        initial_state["metadata"]["tinai"]          = route.tinai
        initial_state["metadata"]["domain"]         = route.domain
        initial_state["metadata"]["kalam_epoch"]    = route.kalam_epoch
        initial_state["metadata"]["router_confidence"] = route.confidence

        # Phase hints injected into task descriptions during execution
        # (handled by _execute_single_task via Uriporuḷ SM below)
        from langchain_core.runnables import RunnableConfig
        config: RunnableConfig = {"configurable": {"session_id": session_id}}

        try:
            final_state: YaazhiState = await self.graph.ainvoke(initial_state, config=config)
        except Exception as exc:
            logfire.error("V4.run: LangGraph pipeline failed", error=str(exc))
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            return YaazhiOutput(
                response=f"V4 pipeline error: {exc}",
                session_id=session_id,
                confidence_score=0.0,
                warnings=[str(exc)],
                processing_time_ms=duration_ms,
                model_used="",
                detected_language="en",
                memories_used=0,
            )

        # ── Step 5: Async Reflection (fire-and-forget) ────────────────
        session_outputs = final_state.get("agent_outputs", {})
        asyncio.create_task(
            reflection_pipeline.run(
                session_outputs = session_outputs,
                tinai           = route.tinai,
                kalam_epoch     = route.kalam_epoch,
                user_id         = settings.default_user_id,
                session_id      = session_id,
            )
        )
        logfire.info("V4.run: Step5 reflection task queued")

        # ── Final output assembly ─────────────────────────────────────
        output_dict = final_state.get("final_output", {})
        duration_ms = int((time.perf_counter() - start_time) * 1000)

        if output_dict:
            result = YaazhiOutput(**output_dict)
            result.processing_time_ms = duration_ms
            result.metadata = {  # type: ignore[attr-defined]
                "tinai":       route.tinai,
                "domain":      route.domain,
                "kalam_epoch": route.kalam_epoch,
            }
            return result

        return YaazhiOutput(
            response="Processing complete but no output was generated.",
            session_id=session_id,
            confidence_score=0.5,
            warnings=["No output from V4 pipeline"],
            processing_time_ms=duration_ms,
            model_used="",
            detected_language="en",
            memories_used=0,
        )
