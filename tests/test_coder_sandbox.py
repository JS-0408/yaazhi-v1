"""tests/test_coder_sandbox.py — Coder agent sandbox tests."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents.coder import CoderAgent, _MAX_CODE_LINES


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_simple_print_executes():
    agent = CoderAgent()
    result = await agent.execute_code('print("hello yaazhi")')
    assert result.success is True
    assert "hello yaazhi" in result.stdout


@pytest.mark.asyncio
async def test_code_too_long_rejected():
    agent = CoderAgent()
    big_code = "x = 1\n" * (_MAX_CODE_LINES + 10)
    result = await agent.execute_code(big_code)
    assert result.success is False
    assert "line" in result.error.lower() or "limit" in result.error.lower()


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_os_system_blocked():
    agent = CoderAgent()
    result = await agent.execute_code("import os\nos.system('ls')")
    assert result.success is False


@pytest.mark.asyncio
async def test_exec_blocked():
    agent = CoderAgent()
    result = await agent.execute_code("exec('print(1)')")
    assert result.success is False


@pytest.mark.asyncio
async def test_subprocess_blocked():
    agent = CoderAgent()
    result = await agent.execute_code("import subprocess\nsubprocess.run(['ls'])")
    assert result.success is False


# ---------------------------------------------------------------------------
# Network isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_network_blocked_in_sandbox():
    """Code that tries to make network calls must fail in the sandbox."""
    agent = CoderAgent()
    code = (
        "import urllib.request\n"
        "urllib.request.urlopen('http://example.com', timeout=2)\n"
        "print('network_ok')"
    )
    result = await agent.execute_code(code)
    # Should either fail (blocked) or not print 'network_ok'
    assert result.success is False or "network_ok" not in result.stdout


# ---------------------------------------------------------------------------
# Tempdir cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sandbox_tempdir_cleaned_up():
    """Sandbox dir must not leak after execution."""
    agent = CoderAgent()
    captured_dir = []

    original_execute = agent.execute_code

    async def spy_execute(code, **kwargs):
        result = await original_execute(code, **kwargs)
        if agent._sandbox_dir:
            captured_dir.append(agent._sandbox_dir)
        return result

    result = await spy_execute("print('test')")
    # After completion, _sandbox_dir should be None (cleaned up)
    assert agent._sandbox_dir is None


# ---------------------------------------------------------------------------
# Exponential backoff on retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix_loop_uses_backoff():
    """Verify asyncio.sleep is called with increasing delays on retry."""
    agent = CoderAgent()
    sleep_calls = []

    async def mock_sleep(delay):
        sleep_calls.append(delay)

    # Code that always fails
    bad_code = "raise ValueError('always fails')"

    with patch("asyncio.sleep", side_effect=mock_sleep):
        with patch.object(agent, "_ask_llm_to_fix", new_callable=AsyncMock,
                          return_value=bad_code):
            result = await agent.execute_code(bad_code)

    # Should have retried with exponential backoff: 1, 2, 4, 8
    assert len(sleep_calls) > 0
    if len(sleep_calls) >= 2:
        assert sleep_calls[1] > sleep_calls[0]


# ---------------------------------------------------------------------------
# Syntax validation
# ---------------------------------------------------------------------------

def test_syntax_validation_catches_error():
    ok, err = CoderAgent.validate_syntax("def foo(:\n    pass")
    assert ok is False
    assert err


def test_syntax_validation_passes_valid_code():
    ok, err = CoderAgent.validate_syntax("def foo():\n    return 42")
    assert ok is True
    assert err == ""
