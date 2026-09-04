"""
Yaazhi V4 — Mutarporuḷ Filter Engine & Context Assembler.

Implements Step 3 of the V4 execution workflow:

  "Before invoking workers, Yaazhi builds a compact context block (2,048-token budget)"

  Algorithm:
    1. Tiṇai pre-filter  — restrict PG vector candidates to matching Tiṇai.
    2. Decisions check   — query decisions_ledger to rule out rejected paths.
    3. Graph traversal   — 2-hop Neo4j query around target concept.
    4. Scoring & ranking — combined relevance score:

       Score = w1·VectorSim + w2·e^(−λΔt) + w3·GraphEdgeWeight

    5. Token-budget trim — fit assembly into max_tokens (default 2,048).

The assembled context block is returned as a compact string ready to be
injected into agent prompts.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import logfire
import tiktoken

from core.router import RouteDecision, TinaiCategory
from memory.graph_store import graph_store


# ─── Scored Candidate ────────────────────────────────────────────────────────

@dataclass(order=True)
class ScoredCandidate:
    """A retrieved memory chunk or graph node with its composite relevance score."""
    score:    float = field(compare=True)
    source:   str   = field(compare=False)
    content:  str   = field(compare=False)
    metadata: dict  = field(compare=False, default_factory=dict)


# ─── Context Assembler ────────────────────────────────────────────────────────

class ContextAssembler:
    """
    Assembles a bounded context block for a planning step using the
    Mutarporuḷ Filter Engine.

    Weights (configurable):
        w1  — vector similarity weight       (default 0.6)
        w2  — recency / time-decay weight    (default 0.25)
        w3  — graph edge weight              (default 0.15)
        λ   — time-decay rate (per day)      (default 0.1)

    Token budget:
        max_tokens — hard cap (default 2,048, matching V4 spec).

    Usage::

        assembler = ContextAssembler()
        block = await assembler.assemble(
            query="fix arch linux suspend",
            route=route_decision,
            max_tokens=2048,
        )
    """

    def __init__(
        self,
        w1: float = 0.60,
        w2: float = 0.25,
        w3: float = 0.15,
        lambda_decay: float = 0.10,
        max_tokens: int = 2048,
    ) -> None:
        self._w1 = w1
        self._w2 = w2
        self._w3 = w3
        self._lambda = lambda_decay
        self._max_tokens = max_tokens
        self._tokenizer = tiktoken.get_encoding("cl100k_base")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def assemble(
        self,
        query: str,
        route: RouteDecision,
        vector_results: Optional[list[dict[str, Any]]] = None,
        rejected_decisions: Optional[list[str]] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Build and return the context block string.

        Args:
            query:               User query / task description.
            route:               RouteDecision containing Tiṇai and domain.
            vector_results:      Pre-fetched vector search results (optional).
                                 Each dict must have 'content', 'score', 'tinai',
                                 and optionally 'created_at' (ISO timestamp).
            rejected_decisions:  List of decision texts that were already deemed
                                 invalid (from decisions_ledger check).
            max_tokens:          Override the instance-level cap.

        Returns:
            Compact context string (≤ max_tokens tokens).
        """
        budget = max_tokens or self._max_tokens
        t_start = time.time()

        candidates: list[ScoredCandidate] = []

        # Step 1 — Vector results + Tiṇai pre-filter
        if vector_results:
            for item in vector_results:
                item_tinai = item.get("tinai", "general_technical")
                tinai_match = (
                    item_tinai == route.tinai
                    or route.tinai == TinaiCategory.GENERAL_TECHNICAL
                )
                if not tinai_match:
                    continue

                vec_sim   = float(item.get("score", 0.5))
                created   = item.get("created_at")
                time_decay = self._time_decay(created)
                graph_w   = float(item.get("graph_edge_weight", 0.5))

                composite = self._composite(vec_sim, time_decay, graph_w)
                candidates.append(ScoredCandidate(
                    score    = composite,
                    source   = item.get("source", "vector"),
                    content  = item.get("content", ""),
                    metadata = {"tinai": item_tinai, "vec_sim": vec_sim},
                ))

        # Step 2 — Decisions check: filter out rejected paths
        if rejected_decisions:
            candidates = [
                c for c in candidates
                if not any(rej.lower() in c.content.lower() for rej in rejected_decisions)
            ]

        # Step 3 — Graph traversal (2-hop around query concept)
        try:
            graph_nodes = await graph_store.traverse(
                concept=query[:60],
                tinai=route.tinai if route.tinai != TinaiCategory.GENERAL_TECHNICAL else None,
                hops=2,
                limit=15,
            )
            for node in graph_nodes:
                content = str(node.get("content", "")).strip()
                if not content:
                    continue
                graph_w   = float(node.get("confidence") or 0.75)
                vec_sim   = 0.5  # No vector sim for graph nodes
                time_decay = self._time_decay(node.get("created_at"))
                composite = self._composite(vec_sim, time_decay, graph_w)
                candidates.append(ScoredCandidate(
                    score    = composite,
                    source   = f"graph:{node.get('label', 'Node')}",
                    content  = content,
                    metadata = {"node_id": node.get("node_id", "")},
                ))
        except Exception as exc:
            logfire.warning("ContextAssembler: graph traversal failed", error=str(exc))

        # Step 4 — Sort by composite score descending
        candidates.sort(reverse=True)

        # Step 5 — Token-budget trim
        context_block = self._trim_to_budget(candidates, budget, query, route)

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "ContextAssembler.assemble complete",
            candidates=len(candidates),
            duration_ms=duration_ms,
            tinai=route.tinai,
        )
        return context_block

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _composite(
        self,
        vec_sim:    float,
        time_decay: float,
        graph_w:    float,
    ) -> float:
        """
        V4 composite relevance score:
            Score = w1·VectorSim + w2·e^(−λΔt) + w3·GraphEdgeWeight
        """
        return (
            self._w1 * vec_sim
            + self._w2 * time_decay
            + self._w3 * graph_w
        )

    def _time_decay(self, created_at: Optional[str]) -> float:
        """
        Compute e^(−λΔt) where Δt is the age of the memory in days.

        Returns 1.0 if created_at is missing (treat as brand-new).
        """
        if not created_at:
            return 1.0
        try:
            from datetime import datetime, timezone
            if created_at.endswith("Z"):
                created_at = created_at[:-1] + "+00:00"
            dt = datetime.fromisoformat(created_at)
            now = datetime.now(timezone.utc)
            delta_days = max((now - dt).total_seconds() / 86400, 0.0)
            return math.exp(-self._lambda * delta_days)
        except Exception:
            return 1.0

    # ------------------------------------------------------------------
    # Token-budget trim
    # ------------------------------------------------------------------

    def _trim_to_budget(
        self,
        candidates: list[ScoredCandidate],
        budget: int,
        query: str,
        route: RouteDecision,
    ) -> str:
        """
        Build the context block string within the token budget.

        Format::

            [CONTEXT — Tiṇai: arch_linux_dev | Kālam: University_Builder_Epoch]
            1. [graph:Fact] <content>  (score: 0.82)
            2. [vector] <content>      (score: 0.74)
            ...
        """
        header = (
            f"[CONTEXT — Tiṇai: {route.tinai} | Kālam: {route.kalam_epoch}]\n"
        )
        lines: list[str] = [header]
        used_tokens = len(self._tokenizer.encode(header))
        reserved    = 10  # safety margin

        for i, cand in enumerate(candidates, start=1):
            line  = f"{i}. [{cand.source}] {cand.content}  (score: {cand.score:.3f})\n"
            toks  = len(self._tokenizer.encode(line))
            if used_tokens + toks + reserved > budget:
                break
            lines.append(line)
            used_tokens += toks

        if len(lines) == 1:
            lines.append("[No relevant context found within token budget]\n")

        return "".join(lines)


# ─── Module-level singleton ───────────────────────────────────────────────────
context_assembler = ContextAssembler()
