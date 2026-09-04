"""
Yaazhi V4 — Uriporuḷ State Machine.

Implements Step 4 of the V4 execution workflow — the LangGraph state-machine
that transitions tasks through the four classical phases:

  Phase 1  (Exploration  / Kuriñci)  — Researcher Agent: targeted search / memory queries.
  Phase 2  (Execution    / Mullai)   — Coder Agent:      write code in local environment.
  Phase 3  (Debugging    / Marutam)  — Reviewer Agent:   analyse failure logs vs decisions_ledger.
  Phase 4  (Resolution   / Neytal)   — Finalizer:        verify, pass to user, commit to memory.

Each inbound SubTask is assigned a UriporuḷPhase based on its TaskType and the
current error/success context.  The phase governs which agent is called and
what metadata is attached to the AgentOutput for downstream reflection.

This module is consumed by the V4Orchestrator (core/v4_orchestrator.py).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import logfire

from core.state import AgentOutput, SubTask, TaskType


# ─── Classical Phase Enum ────────────────────────────────────────────────────

class UriporuḷPhase(str, Enum):
    """
    The four Uriporuḷ execution phases.

    Named after the four classical Tiṇai landscapes used in Sangam poetics
    as emotional metaphors — repurposed here as execution state labels.
    """
    KURINCHI  = "kurinchi"   # Exploration — mountain / discovery
    MULLAI    = "mullai"     # Execution   — forest / routine
    MARUTAM   = "marutam"    # Debugging   — farmland / conflict resolution
    NEYTAL    = "neytal"     # Resolution  — seashore / longing / completion


# ─── Phase Router ────────────────────────────────────────────────────────────

class UriporuḷStateMachine:
    """
    Maps tasks and execution context to UriporuḷPhases.

    Stateless — can be called concurrently by the worker pool.

    Usage::

        sm = UriporuḷStateMachine()
        phase = sm.assign_phase(task, prior_error=None)
        # => UriporuḷPhase.MULLAI  (for CODE task, no error)
    """

    # Task-type → default phase mapping
    _DEFAULT_PHASE_MAP: dict[TaskType, UriporuḷPhase] = {
        TaskType.RESEARCH: UriporuḷPhase.KURINCHI,
        TaskType.MEMORY:   UriporuḷPhase.KURINCHI,
        TaskType.READ_DOC: UriporuḷPhase.KURINCHI,
        TaskType.BROWSE:   UriporuḷPhase.KURINCHI,
        TaskType.CODE:     UriporuḷPhase.MULLAI,
        TaskType.NOTIFY:   UriporuḷPhase.MULLAI,
        TaskType.VOICE:    UriporuḷPhase.MULLAI,
        TaskType.PRIVATE:  UriporuḷPhase.MULLAI,
        TaskType.REVIEW:   UriporuḷPhase.MARUTAM,
    }

    def assign_phase(
        self,
        task:        SubTask,
        prior_error: Optional[str] = None,
        retry_count: int = 0,
    ) -> UriporuḷPhase:
        """
        Determine the execution phase for a SubTask.

        Logic:
          - If retry_count > 0 and there is a prior_error → Marutam (debugging).
          - If task is a code/execution type → Mullai.
          - If task is a research/discovery type → Kuriñci.
          - Resolution (Neytal) is assigned externally by the finalizer node.

        Args:
            task:        The SubTask to assign a phase to.
            prior_error: Error message from a previous failed attempt.
            retry_count: How many times this task has been retried.

        Returns:
            UriporuḷPhase appropriate for the current execution context.
        """
        # Error recovery mode → Marutam
        if prior_error and retry_count > 0:
            logfire.debug(
                "UriporuḷSM: Marutam (debug) phase assigned",
                task_id=task.task_id[:8],
                retry=retry_count,
            )
            return UriporuḷPhase.MARUTAM

        phase = self._DEFAULT_PHASE_MAP.get(task.task_type, UriporuḷPhase.MULLAI)
        logfire.debug(
            "UriporuḷSM: phase assigned",
            task_id=task.task_id[:8],
            task_type=task.task_type.value,
            phase=phase.value,
        )
        return phase

    def phase_to_agent_hint(self, phase: UriporuḷPhase) -> str:
        """
        Return a short system-prompt hint for the agent based on the active phase.

        Injected as a prefix into agent prompts to steer behaviour.
        """
        hints: dict[UriporuḷPhase, str] = {
            UriporuḷPhase.KURINCHI: (
                "[Kuriñci — Exploration] Perform targeted discovery. "
                "Surface relevant facts, prior decisions, and memory context. "
                "Be concise and citation-heavy."
            ),
            UriporuḷPhase.MULLAI: (
                "[Mullai — Execution] Write production-quality output. "
                "Prefer local tools. No unnecessary commentary."
            ),
            UriporuḷPhase.MARUTAM: (
                "[Marutam — Debugging] Analyse the failure. "
                "Cross-reference with prior decisions_ledger entries. "
                "Propose a minimal corrective diff, not a full rewrite."
            ),
            UriporuḷPhase.NEYTAL: (
                "[Neytal — Resolution] Verify correctness and completeness. "
                "Summarise what was done and commit key facts to memory."
            ),
        }
        return hints.get(phase, "")

    def annotate_output(
        self,
        output: AgentOutput,
        phase:  UriporuḷPhase,
    ) -> AgentOutput:
        """
        Attach phase metadata to an AgentOutput for the reflection pipeline.

        Mutates `output.metadata` in-place — safe because AgentOutput is
        a Pydantic model with a mutable dict field.

        Returns:
            The same AgentOutput with phase injected into metadata.
        """
        output.metadata["uriporul_phase"] = phase.value
        return output


# ─── Module-level singleton ───────────────────────────────────────────────────
uriporul_sm = UriporuḷStateMachine()
