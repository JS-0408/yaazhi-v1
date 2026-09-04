"""
Yaazhi V4 — Test Suite.

Tests all new V4 modules:
  - CognitiveRouter (core/router.py)
  - AkamStore (memory/akam_store.py)
  - ContextAssembler (core/context_assembler.py)
  - UriporuḷStateMachine (core/uriporul.py)
  - GraphStore (memory/graph_store.py) — mocked
  - EpisodicEventLogger (memory/event_logger.py) — mocked
  - ReflectionPipeline (agents/reflection_v4.py) — mocked

All external I/O (PG, Redis, Neo4j) is mocked via pytest monkeypatch / AsyncMock.
No live services are required.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── CognitiveRouter ─────────────────────────────────────────────────────────

from core.router import (
    CognitiveRouter,
    Domain,
    KalamEpoch,
    RouteDecision,
    TinaiCategory,
)


class TestCognitiveRouter:
    """Tests for the Mutarporuḷ domain router."""

    def setup_method(self):
        self.router = CognitiveRouter()

    def test_puram_code_task(self):
        decision = self.router.classify("write a Python script to parse CSV files")
        assert decision.domain == Domain.PURAM
        assert decision.confidence > 0.5
        assert decision.puram_layers == [1, 2, 5, 6]
        assert decision.akam_layers  == []

    def test_akam_burnout_query(self):
        decision = self.router.classify("I feel burnout, check my stress level and preferences")
        assert decision.domain == Domain.AKAM
        assert decision.akam_layers == [4, 7, 11, 12]
        assert decision.puram_layers == []

    def test_arch_linux_tinai(self):
        decision = self.router.classify("debug my arch linux pacman and systemd service")
        assert decision.tinai == TinaiCategory.ARCH_LINUX_DEV

    def test_bms_ml_tinai(self):
        decision = self.router.classify("train the BMS battery SoC ML model")
        assert decision.tinai == TinaiCategory.BMS_ML_PROJECT

    def test_default_tinai_general(self):
        decision = self.router.classify("hello world")
        assert decision.tinai == TinaiCategory.GENERAL_TECHNICAL

    def test_kalam_epoch_override(self):
        decision = self.router.classify("do something", kalam_epoch=KalamEpoch.RESEARCH_SPRINT)
        assert decision.kalam_epoch == KalamEpoch.RESEARCH_SPRINT

    def test_default_kalam_epoch(self):
        decision = self.router.classify("do something")
        assert decision.kalam_epoch == KalamEpoch.UNIVERSITY_BUILDER

    def test_confidence_in_range(self):
        decision = self.router.classify("implement a BMS ML model in Python")
        assert 0.0 <= decision.confidence <= 1.0

    def test_route_decision_is_model(self):
        decision = self.router.classify("research quantum computing")
        assert isinstance(decision, RouteDecision)
        assert decision.reasoning != ""

    def test_puram_default_on_no_signals(self):
        """Neutral input with no domain signals should default to Puram."""
        decision = self.router.classify("123 xyz")
        assert decision.domain == Domain.PURAM


# ─── AkamState ───────────────────────────────────────────────────────────────

from memory.akam_store import AkamState, AkamStore


class TestAkamState:
    """Tests for AkamState context block serialisation."""

    def _make_state(self, burnout: float = 0.3) -> AkamState:
        return AkamState(
            state_id           = "test-id",
            burnout_score      = burnout,
            preferred_tone     = "Dry, Concise",
            active_constraints = {"hardware": "local", "llm": "ollama"},
            identity_summary   = "Developer in University_Builder_Epoch",
            meta_epochs        = [{"epoch": "University_Builder_Epoch"}],
            objectives         = ["Complete B.Tech", "Ship BMS"],
        )

    def test_context_block_contains_burnout(self):
        state = self._make_state(burnout=0.75)
        block = state.to_context_block()
        assert "0.75" in block
        assert "High" in block

    def test_context_block_contains_tone(self):
        state = self._make_state()
        block = state.to_context_block()
        assert "Dry, Concise" in block

    def test_context_block_respects_max_chars(self):
        state = self._make_state()
        block = state.to_context_block(max_chars=50)
        assert len(block) <= 50

    def test_burnout_label_critical(self):
        state = self._make_state(burnout=0.9)
        block = state.to_context_block()
        assert "Critical" in block

    def test_burnout_label_low(self):
        state = self._make_state(burnout=0.1)
        block = state.to_context_block()
        assert "Low" in block

    def test_repr(self):
        state = self._make_state()
        assert "AkamState" in repr(state)


# ─── ContextAssembler ────────────────────────────────────────────────────────

from core.context_assembler import ContextAssembler, ScoredCandidate
from core.router import RouteDecision


class TestContextAssembler:
    """Tests for Mutarporuḷ Filter Engine & compositing."""

    def setup_method(self):
        self.assembler = ContextAssembler(max_tokens=512)
        self.route = RouteDecision(
            domain=Domain.PURAM,
            tinai=TinaiCategory.GENERAL_TECHNICAL,
            kalam_epoch=KalamEpoch.UNIVERSITY_BUILDER,
        )

    def test_composite_score_sums_weights(self):
        score = self.assembler._composite(1.0, 1.0, 1.0)
        expected = 0.60 + 0.25 + 0.15
        assert abs(score - expected) < 1e-9

    def test_time_decay_fresh_memory(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        decay = self.assembler._time_decay(now_iso)
        # Very recent → close to 1.0
        assert decay > 0.99

    def test_time_decay_old_memory(self):
        old_iso = "2020-01-01T00:00:00+00:00"
        decay = self.assembler._time_decay(old_iso)
        # Old memory → decay significantly
        assert decay < 0.5

    def test_time_decay_none(self):
        assert self.assembler._time_decay(None) == 1.0

    @pytest.mark.asyncio
    async def test_assemble_returns_string(self):
        with patch("core.context_assembler.graph_store") as mock_gs:
            mock_gs.traverse = AsyncMock(return_value=[])
            block = await self.assembler.assemble(
                query=  "test query",
                route=  self.route,
                vector_results=[
                    {"content": "Sample memory", "score": 0.9,
                     "tinai": "general_technical", "source": "test"}
                ],
            )
        assert isinstance(block, str)
        assert "CONTEXT" in block

    @pytest.mark.asyncio
    async def test_assemble_tinai_filter(self):
        """Candidates with wrong Tiṇai are excluded."""
        route = RouteDecision(
            domain=Domain.PURAM,
            tinai=TinaiCategory.ARCH_LINUX_DEV,
            kalam_epoch=KalamEpoch.UNIVERSITY_BUILDER,
        )
        with patch("core.context_assembler.graph_store") as mock_gs:
            mock_gs.traverse = AsyncMock(return_value=[])
            block = await self.assembler.assemble(
                query="arch linux",
                route=route,
                vector_results=[
                    # Wrong Tiṇai — should be filtered out
                    {"content": "BMS training run", "score": 0.95,
                     "tinai": "bms_ml_project", "source": "test"},
                    # Correct Tiṇai — should be included
                    {"content": "pacman -Syu updated 200 packages", "score": 0.85,
                     "tinai": "arch_linux_dev", "source": "test"},
                ],
            )
        assert "pacman" in block
        assert "BMS training run" not in block

    @pytest.mark.asyncio
    async def test_assemble_empty_returns_placeholder(self):
        with patch("core.context_assembler.graph_store") as mock_gs:
            mock_gs.traverse = AsyncMock(return_value=[])
            block = await self.assembler.assemble(
                query="test", route=self.route, vector_results=[]
            )
        assert "No relevant context" in block


# ─── UriporuḷStateMachine ────────────────────────────────────────────────────

from core.uriporul import UriporuḷPhase, UriporuḷStateMachine
from core.state import AgentOutput, SubTask, TaskType


class TestUriporuḷStateMachine:
    """Tests for the Uriporuḷ phase router."""

    def setup_method(self):
        self.sm = UriporuḷStateMachine()

    def _make_task(self, task_type: TaskType) -> SubTask:
        return SubTask(task_type=task_type, description="test task")

    def test_code_task_mullai(self):
        task = self._make_task(TaskType.CODE)
        assert self.sm.assign_phase(task) == UriporuḷPhase.MULLAI

    def test_research_task_kurinchi(self):
        task = self._make_task(TaskType.RESEARCH)
        assert self.sm.assign_phase(task) == UriporuḷPhase.KURINCHI

    def test_memory_task_kurinchi(self):
        task = self._make_task(TaskType.MEMORY)
        assert self.sm.assign_phase(task) == UriporuḷPhase.KURINCHI

    def test_error_retry_marutam(self):
        task = self._make_task(TaskType.CODE)
        phase = self.sm.assign_phase(task, prior_error="SyntaxError", retry_count=1)
        assert phase == UriporuḷPhase.MARUTAM

    def test_review_task_marutam(self):
        task = self._make_task(TaskType.REVIEW)
        assert self.sm.assign_phase(task) == UriporuḷPhase.MARUTAM

    def test_phase_hints_not_empty(self):
        for phase in UriporuḷPhase:
            hint = self.sm.phase_to_agent_hint(phase)
            assert len(hint) > 0
            assert phase.value.capitalize() in hint or phase.value.lower() in hint.lower()

    def test_annotate_output_injects_phase(self):
        output = AgentOutput(agent_name="coder", task_id="t1", content="result")
        self.sm.annotate_output(output, UriporuḷPhase.MULLAI)
        assert output.metadata["uriporul_phase"] == "mullai"


# ─── GraphStore (mocked) ─────────────────────────────────────────────────────

from memory.graph_store import GraphStore, NodeLabel, RelType


class TestGraphStoreMocked:
    """Tests for GraphStore with Neo4j driver mocked out."""

    def setup_method(self):
        self.gs = GraphStore()
        self.gs._password = "test"  # ensure driver attempts init

    @pytest.mark.asyncio
    async def test_add_fact_no_driver_returns_none(self):
        """When no Neo4j is reachable, add_fact returns None gracefully."""
        self.gs._driver = None
        self.gs._password = ""  # prevent actual connection
        result = await self.gs.add_fact("test fact")
        assert result is None

    @pytest.mark.asyncio
    async def test_traverse_no_driver_returns_empty(self):
        self.gs._driver = None
        self.gs._password = ""
        result = await self.gs.traverse("arch linux")
        assert result == []

    @pytest.mark.asyncio
    async def test_ping_no_driver_returns_false(self):
        self.gs._driver = None
        self.gs._password = ""
        result = await self.gs.ping()
        assert result is False


# ─── EpisodicEventLogger (mocked) ────────────────────────────────────────────

from memory.event_logger import EpisodicEventLogger


class TestEpisodicEventLogger:
    """Tests for EpisodicEventLogger with PG mocked out."""

    def setup_method(self):
        self.logger = EpisodicEventLogger()

    @pytest.mark.asyncio
    async def test_log_no_pg_returns_none(self):
        """Without PG pool, log() returns None silently."""
        self.logger._pg_pool = None
        with patch("config.settings.settings") as mock_settings:
            mock_settings.postgres_url = ""
            result = await self.logger.log(summary="test event")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_recent_no_pg_returns_empty(self):
        self.logger._pg_pool = None
        result = await self.logger.fetch_recent()
        assert result == []

    @pytest.mark.asyncio
    async def test_log_with_mock_pool(self):
        """With a mocked PG pool, log() inserts and returns an event_id."""
        mock_conn  = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=None)
        mock_pool  = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        self.logger._pg_pool = mock_pool

        result = await self.logger.log(
            tinai="arch_linux_dev",
            event_type="code_execution",
            summary="compiled kernel module",
        )
        assert result is not None
        assert len(result) == 36  # UUID length


# ─── ReflectionPipeline (mocked) ─────────────────────────────────────────────

from agents.reflection_v4 import ReflectionPipeline


class TestReflectionPipeline:
    """Tests for the async reflection pipeline with all I/O mocked."""

    def setup_method(self):
        self.pipeline = ReflectionPipeline()

    @pytest.mark.asyncio
    async def test_run_empty_outputs(self):
        """run() with empty session_outputs completes without error."""
        with (
            patch("agents.reflection_v4.event_logger") as mock_el,
            patch("agents.reflection_v4.akam_store")  as mock_akam,
            patch("agents.reflection_v4.graph_store")  as mock_gs,
        ):
            mock_el.log = AsyncMock(return_value="event-id")
            mock_el.fetch_recent = AsyncMock(return_value=[])
            mock_akam.update_burnout = AsyncMock(return_value=0.1)
            mock_gs.add_fact = AsyncMock(return_value="fact-id")

            await self.pipeline.run(session_outputs={}, tinai="general_technical")

    @pytest.mark.asyncio
    async def test_affective_update_called_with_counts(self):
        """Step 4 update_burnout receives correct command_count and error_count."""
        session_outputs = {
            "t1": {"agent_name": "coder",      "success": True,  "content": "ok"},
            "t2": {"agent_name": "researcher",  "success": False, "content": "fail"},
            "t3": {"agent_name": "coder",       "success": True,  "content": "ok"},
        }
        with (
            patch("agents.reflection_v4.event_logger") as mock_el,
            patch("agents.reflection_v4.akam_store")  as mock_akam,
            patch("agents.reflection_v4.graph_store")  as mock_gs,
        ):
            mock_el.log = AsyncMock(return_value="eid")
            mock_el.fetch_recent = AsyncMock(return_value=[])
            mock_akam.update_burnout = AsyncMock(return_value=0.15)
            mock_gs.add_fact = AsyncMock(return_value="fid")

            await self.pipeline.run(session_outputs=session_outputs)

            mock_akam.update_burnout.assert_called_once_with(
                command_count=3,
                error_count=1,
                user_id=None,
            )

    @pytest.mark.asyncio
    async def test_fact_abstraction_triggered_on_recurring_events(self):
        """Step 2 creates a :Fact when an event_type recurs >= 3 times."""
        recurring_events = [
            {"event_type": "code_execution", "summary": "compiled kernel module", "tinai": "arch_linux_dev"}
        ] * 4  # 4 occurrences ≥ threshold

        with (
            patch("agents.reflection_v4.event_logger") as mock_el,
            patch("agents.reflection_v4.akam_store")  as mock_akam,
            patch("agents.reflection_v4.graph_store")  as mock_gs,
        ):
            mock_el.log = AsyncMock(return_value="eid")
            mock_el.fetch_recent = AsyncMock(return_value=recurring_events)
            mock_akam.update_burnout = AsyncMock(return_value=0.1)
            mock_gs.add_fact = AsyncMock(return_value="new-fact-id")
            mock_gs.update_edge_weight = AsyncMock(return_value=None)

            await self.pipeline._step2_fact_abstraction(
                tinai="arch_linux_dev", kalam_epoch="University_Builder_Epoch"
            )

            mock_gs.add_fact.assert_called_once()
            call_kwargs = mock_gs.add_fact.call_args.kwargs
            assert "code_execution" in call_kwargs["text"]
            assert call_kwargs["confidence"] >= 0.85
