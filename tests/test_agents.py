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

    def test_cache_key_full_sha256(self):
        from agents.researcher import ResearcherAgent
        agent = ResearcherAgent()
        key = agent._cache_key("test query")
        assert key.startswith("yaazhi:research:")
        # Full 64-char SHA256 digest
        assert len(key.split(":")[-1]) == 64

    @pytest.mark.asyncio
    async def test_deep_fetch_ssrf_blocked(self):
        from agents.researcher import ResearcherAgent
        agent = ResearcherAgent()
        res = await agent.deep_fetch("http://169.254.169.254/latest/meta-data/")
        assert "blocked by SSRF filter" in res.lower() or "ssrf" in res.lower()


# ─────────────────────────────────────────────────────────
# SSRF & Browser Agent
# ─────────────────────────────────────────────────────────

class TestBrowserAgentAndSSRF:

    def test_validate_url_blocks_private_ips(self):
        from agents.browser import validate_url
        blocked_urls = [
            "http://127.0.0.1:8000/admin",
            "http://localhost:5000",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
        ]
        for url in blocked_urls:
            valid, reason = validate_url(url)
            assert valid is False
            assert "blocked" in reason.lower() or "private" in reason.lower() or "loopback" in reason.lower()

    def test_validate_url_blocks_invalid_schemes(self):
        from agents.browser import validate_url
        valid, reason = validate_url("file:///etc/passwd")
        assert valid is False
        assert "scheme" in reason.lower()

    def test_validate_url_allows_public_urls(self):
        from agents.browser import validate_url
        valid, reason = validate_url("https://python.org")
        assert valid is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_browser_agent_close_idempotent(self):
        from agents.browser import BrowserAgent
        agent = BrowserAgent()
        await agent.close()
        await agent.close()  # Must not crash
        assert agent._closed is True


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
        with pytest.raises((PermissionError, ValueError, RuntimeError, GuardrailViolation)):
            CoderAgent().execute_safe("import os; os.system('rm -rf /')")

    def test_validate_syntax_valid(self):
        from agents.coder import CoderAgent
        ok, err = CoderAgent.validate_syntax("x = [i for i in range(10)]")
        assert ok is True
        assert err == ""

    def test_validate_syntax_invalid(self):
        from agents.coder import CoderAgent
        ok, err = CoderAgent.validate_syntax("def broken_func(")
        assert ok is False
        assert "SyntaxError" in err

    @pytest.mark.asyncio
    async def test_code_over_500_lines_rejected(self):
        from agents.coder import CoderAgent
        long_code = "\n".join(["x = 1"] * 505)
        agent = CoderAgent()
        result = await agent.execute_code(long_code)
        assert result.success is False
        assert "500-line limit" in result.stderr

    @pytest.mark.asyncio
    async def test_dangerous_pattern_eval_exec_blocked(self):
        from agents.coder import CoderAgent
        agent = CoderAgent()
        result = await agent.execute_code("eval('2 + 2')")
        assert result.success is False
        assert any(k in result.stderr.lower() for k in ["banned", "unsafe", "blocked", "isolated"])

    @pytest.mark.asyncio
    @patch("litellm.acompletion")
    async def test_write_code_strips_markdown_fences(self, mock_acompletion):
        mock_acompletion.return_value.choices = [
            MagicMock(message=MagicMock(content="```python\ndef add(a, b):\n    return a + b\n```"))
        ]
        from agents.coder import CoderAgent
        code = await CoderAgent().write_code("Write add function")
        assert code == "def add(a, b):\n    return a + b"


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
        assert result is not None
        assert hasattr(result, "response") or (isinstance(result, dict) and "response" in result)
