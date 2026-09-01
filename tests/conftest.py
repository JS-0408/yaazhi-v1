"""
tests/conftest.py — Shared fixtures for the Yaazhi test suite.

All external services (Redis, ChromaDB, LLMs, Playwright) are mocked.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

try:
    import fakeredis.aioredis as fakeredis_aioredis
except ImportError:  # pragma: no cover
    fakeredis_aioredis = None

from core.state import YaazhiState


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------

@pytest.fixture
def event_loop():
    """Provide a fresh event loop per test to avoid cross-test leakage."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture
def sample_wav_bytes():
    """Small dummy WAV bytes used by STT/TTS tests."""
    # Minimal RIFF header + WAVE chunk - tests only need some bytes
    return b"RIFF....WAVEfmt "


@pytest.fixture(autouse=True)
def stub_prometheus_client(monkeypatch):
    """Ensure prometheus_client is available in tests (stubbed) so modules importing it don't fail."""
    import sys
    from unittest.mock import MagicMock

    if "prometheus_client" not in sys.modules:
        monkeypatch.setitem(sys.modules, "prometheus_client", MagicMock())


# ---------------------------------------------------------------------------
# Settings override
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_settings(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("YAAZHI_API_KEY", "test-api-key")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("POSTGRES_URL", "")
    monkeypatch.setenv("DEFAULT_USER_ID", "test_user")
    monkeypatch.setenv("ENV", "development")


# ---------------------------------------------------------------------------
# Redis mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    if fakeredis_aioredis is not None:
        return fakeredis_aioredis.FakeRedis()

    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.set = AsyncMock(return_value=True)
    redis_mock.incr = AsyncMock(return_value=1)
    redis_mock.expire = AsyncMock(return_value=True)
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.scan = AsyncMock(return_value=(0, []))
    redis_mock.lrange = AsyncMock(return_value=[])
    redis_mock.rpush = AsyncMock(return_value=1)
    redis_mock.ltrim = AsyncMock(return_value=True)
    redis_mock.pipeline = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=AsyncMock()),
        __aexit__=AsyncMock(return_value=False),
        execute=AsyncMock(return_value=[1, True, True]),
        rpush=MagicMock(), ltrim=MagicMock(), expire=MagicMock(),
    ))
    return redis_mock


@pytest.fixture(autouse=True)
def patch_redis_from_url(monkeypatch, mock_redis):
    monkeypatch.setattr("redis.asyncio.from_url", lambda *args, **kwargs: mock_redis)
    return mock_redis


# Autouse fixture to disable or stub external vector DB clients during unit tests.
# Prevents tests from attempting to connect to real ChromaDB/pgvector instances.
@pytest.fixture(autouse=True)
def disable_external_vector_backends(monkeypatch):
    try:
        import memory.vector_store as vs_mod
        # Force the module to treat ChromaDB as unavailable so it won't try network calls
        monkeypatch.setattr(vs_mod, "_CHROMA_AVAILABLE", False)
    except Exception:
        pass
    # Also stub any top-level chromadb module to avoid accidental imports/initialisation
    import sys
    from unittest.mock import MagicMock as _MagicMock
    monkeypatch.setitem(sys.modules, "chromadb", _MagicMock())
    return None


@pytest.fixture
def mock_vector_store():
    vs = AsyncMock()
    vs.search = AsyncMock(return_value=[])
    vs.add = AsyncMock(return_value="mem-001")
    vs.ping = AsyncMock(return_value=True)
    return vs


# ---------------------------------------------------------------------------
# ChromaDB mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_chroma_collection():
    coll = MagicMock()
    coll.add = MagicMock(return_value=None)
    coll.query = MagicMock(return_value={
        "ids": [["mem-001"]],
        "documents": [["Test memory content"]],
        "metadatas": [[{"source": "test", "created_at": "2026-05-10T00:00:00+00:00"}]],
        "distances": [[0.1]],
    })
    coll.count = MagicMock(return_value=42)
    coll.delete = MagicMock(return_value=None)
    coll.get = MagicMock(return_value={"ids": [], "documents": [], "metadatas": []})
    return coll


# ---------------------------------------------------------------------------
# LLM mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_response():
    choice = MagicMock()
    choice.message.content = "Test LLM response"
    response = MagicMock()
    response.choices = [choice]
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 20
    return response


@pytest.fixture
def mock_groq_llm(mock_llm_response):
    with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = mock_llm_response
        yield mock_llm


# ---------------------------------------------------------------------------
# Sample state
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_state() -> YaazhiState:
    return {
        "session_id": "test-session-001",
        "user_input": "What is the capital of India?",
        "recalled_context": "",
        "task_plan": None,
        "subtask_results": [],
        "final_response": "",
        "iteration_count": 0,
        "metadata": {},
        "revise_reason": "",
        "revise_score": 0.0,
        "errors": [],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# FastAPI TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def test_client():
    from api.main import create_app
    app = create_app()

    # Inject mock state
    app.state.yaazhi = AsyncMock()
    app.state.yaazhi.run = AsyncMock(return_value=MagicMock(
        response="Test response",
        session_id="test-session",
        memories_used=0,
        agents_called=["researcher"],
        model_used="groq/llama-3.3-70b-versatile",
        detected_language="en",
        warnings=[],
    ))
    app.state.retriever = AsyncMock()
    app.state.retriever.build_context = AsyncMock(return_value="Test context")
    app.state.episodic = AsyncMock()
    app.state.episodic.add_message = AsyncMock()
    app.state.episodic._redis = AsyncMock()
    app.state.episodic._ensure_redis = AsyncMock()
    app.state.vector_store = AsyncMock()
    app.state.ingester = AsyncMock()
    app.state.stt_engine = AsyncMock()
    app.state.tts_engine = AsyncMock()

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
