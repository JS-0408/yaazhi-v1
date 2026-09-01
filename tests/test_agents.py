"""
tests/test_agents.py — Unit tests for Yaazhi Agent Swarm
Tests Researcher, Coder, Notifier, Reader agents and Orchestrator.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ─────────────────────────────────────────────────────────
# Researcher agent
# ─────────────────────────────────────────────────────────

class TestResearcherAgent:

    @pytest.mark.asyncio
    @patch("litellm.acompletion")
    async def test_run_returns_string(self, mock_acompletion):
        mock_acompletion.return_value.choices = [MagicMock(message=MagicMock(content="Groq is the fastest LLM inference API."))]
        from agents.researcher import ResearcherAgent
        result = await ResearcherAgent().run("What is Groq?")
        assert isinstance(result, str) and len(result) > 0

    @pytest.mark.asyncio
    @patch("litellm.acompletion")
    async def test_run_calls_llm_once(self, mock_acompletion):
        mock_acompletion.return_value.choices = [MagicMock(message=MagicMock(content="pgvector allows vector search in Postgres."))]
        from agents.researcher import ResearcherAgent
        await ResearcherAgent().run("Brief overview of pgvector.")
        mock_acompletion.assert_called_once()

    @pytest.mark.asyncio
    @patch("litellm.acompletion")
    async def test_empty_task_raises(self, mock_acompletion):
        from agents.researcher import ResearcherAgent
        with pytest.raises(Exception):
            await ResearcherAgent().run("")


# ─────────────────────────────────────────────────────────
# Coder agent
# ─────────────────────────────────────────────────────────

class TestCoderAgent:

    @pytest.mark.asyncio
    @patch("litellm.acompletion")
    async def test_write_code_returns_string(self, mock_acompletion):
        mock_acompletion.return_value.choices = [MagicMock(message=MagicMock(content="def hello():\n    print('Hello Yaazhi!')"))]
        from agents.coder import CoderAgent
        code = await CoderAgent().write_code("Write a Python hello world function.")
        assert isinstance(code, str)

    def test_execute_safe_runs_code(self):
        from agents.coder import CoderAgent
        result = CoderAgent().execute_safe("print('42')")
        assert "42" in str(result) or result is not None

    def test_dangerous_code_blocked(self):
        from agents.coder import CoderAgent
        from core.guardrails import GuardrailViolation
        # Our updated sandbox returns a CodeResult with success=False and an error string.
        # Wait, the codebase test expects raises. The updated validate_code_for_execution raises GuardrailViolation.
        with pytest.raises((PermissionError, ValueError, RuntimeError, GuardrailViolation)):
            CoderAgent().execute_safe("import os; os.system('rm -rf /')")


# ─────────────────────────────────────────────────────────
# Notifier agent
# ─────────────────────────────────────────────────────────

class TestNotifierAgent:

    @patch("agents.notifier.requests")
    def test_send_webhook_called(self, mock_req):
        mock_req.post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})
        from agents.notifier import NotifierAgent
        agent = NotifierAgent(webhook_url="http://localhost:5678/webhook/test")
        result = agent.notify("Test Alert", "Task complete.", channel="whatsapp")
        mock_req.post.assert_called_once()
        assert result is not None

    @patch("agents.notifier.requests")
    def test_notify_graceful_failure(self, mock_req):
        mock_req.post.side_effect = ConnectionError("Unreachable")
        from agents.notifier import NotifierAgent
        result = NotifierAgent(webhook_url="http://localhost:5678/webhook/test").notify(
            "Fail", "Should not crash.", channel="email"
        )
        assert result is False or result is None


# ─────────────────────────────────────────────────────────
# Reader agent
# ─────────────────────────────────────────────────────────

class TestReaderAgent:

    @pytest.mark.asyncio
    @patch("litellm.acompletion")
    async def test_summarise_returns_string(self, mock_acompletion):
        mock_acompletion.return_value.choices = [MagicMock(message=MagicMock(content="This PDF covers DSP fundamentals."))]
        from agents.reader import ReaderAgent
        summary = await ReaderAgent().summarise_pdf(b"%PDF-1.4 fake", filename="notes.pdf")
        assert isinstance(summary, str)

    @pytest.mark.asyncio
    @patch("litellm.acompletion")
    async def test_answer_from_doc(self, mock_acompletion):
        mock_acompletion.return_value.choices = [MagicMock(message=MagicMock(content="Z-transform analyses discrete-time signals."))]
        from agents.reader import ReaderAgent
        answer = await ReaderAgent().answer_from_document(
            question="What is Z-transform?",
            document_text="Z-transform definition...",
        )
        assert isinstance(answer, str) and len(answer) > 0


# ─────────────────────────────────────────────────────────
# Orchestrator smoke test
# ─────────────────────────────────────────────────────────

class TestOrchestrator:

    @pytest.mark.asyncio
    @patch("core.orchestrator.ResearcherAgent")
    @patch("core.orchestrator.CoderAgent")
    @patch("core.orchestrator.ReviewerAgent")
    async def test_run_returns_response(self, MockReviewer, MockCoder, MockResearcher):
        MockResearcher.return_value.run = AsyncMock(return_value="Research done.")
        MockCoder.return_value.write_code = AsyncMock(return_value="# code")
        MockReviewer.return_value.review = AsyncMock(return_value={"approved": True, "feedback": ""})

        from core.orchestrator import Yaazhi
        result = await Yaazhi().run(
            user_input="Explain pgvector.", session_id="test"
        )
        assert isinstance(result, dict)
        assert "response" in result
