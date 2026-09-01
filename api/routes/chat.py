"""
Yaazhi Chat API Routes — production-hardened.

Audit fixes applied (2026-05-10):
  API-2 : recalled_context is now passed into yaazhi.run() as context parameter.
  API-3 : list_sessions reuses app.state.episodic._redis (no new connection per request).
  SEC   : Voice sessions stored in episodic memory after response.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import AsyncGenerator

import logfire
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.state import YaazhiOutput
from memory.episodic import EpisodicMemory
from memory.retriever import SemanticRetriever

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """Incoming chat request payload."""

    message: str = Field(..., min_length=1, max_length=2000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    language: str = Field(default="auto")
    stream: bool = Field(default=False)


class ChatResponse(BaseModel):
    """Chat response payload."""

    response: str
    session_id: str
    memories_used: int = Field(default=0)
    agents_called: list[str] = Field(default_factory=list)
    processing_time_ms: int = Field(default=0)
    model_used: str = Field(default="")
    detected_language: str = Field(default="en")
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------

async def _sse_generator(text: str) -> AsyncGenerator[bytes, None]:
    """Yield SSE-formatted byte chunks for streaming (word-level, 5 words/chunk)."""
    words = text.split(" ")
    buffer: list[str] = []
    for word in words:
        buffer.append(word)
        if len(buffer) >= 5:
            chunk = " ".join(buffer) + " "
            yield f"data: {chunk}\n\n".encode("utf-8")
            buffer = []
            await asyncio.sleep(0.02)
    if buffer:
        yield f"data: {' '.join(buffer)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/chat", response_model=ChatResponse, summary="Send a message to Yaazhi")
async def chat(request: Request, body: ChatRequest) -> ChatResponse | StreamingResponse:
    """
    Process a user message through the full Yaazhi orchestrator pipeline.

    Memory context is retrieved THEN injected into the orchestrator (API-2 fix).
    Both the user message and assistant response are stored in episodic memory.
    """
    logfire.debug("POST /chat", session_id=body.session_id[:8], stream=body.stream)
    t_start = time.time()

    state = request.app.state
    if not hasattr(state, "yaazhi"):
        raise HTTPException(status_code=503, detail="Yaazhi core not initialised")

    yaazhi = state.yaazhi
    retriever: SemanticRetriever = state.retriever
    episodic: EpisodicMemory = state.episodic

    # ── Retrieve memory context ───────────────────────────────────────────────
    recalled_context = ""
    try:
        recalled_context = await retriever.build_context(body.message, max_tokens=1500)
    except Exception as exc:
        logfire.warning("Memory retrieval failed during chat", error=str(exc))

    # ── Store user message in episodic memory (before orchestrator) ───────────
    try:
        await episodic.add_message(body.session_id, "user", body.message)
    except Exception as exc:
        logfire.warning("Episodic: failed to store user message", error=str(exc))

    # ── Run orchestrator — inject context (API-2 FIX) ─────────────────────────
    try:
        output: YaazhiOutput = await yaazhi.run(
            body.message,
            body.session_id,
            context=recalled_context,          # API-2: context now injected
        )
    except Exception as exc:
        logfire.error("Orchestrator run failed", session_id=body.session_id[:8], error=str(exc))
        raise HTTPException(status_code=500, detail=f"Orchestrator error: {exc}") from exc

    # ── Store assistant response in episodic memory ───────────────────────────
    try:
        await episodic.add_message(body.session_id, "assistant", output.response)
    except Exception as exc:
        logfire.warning("Episodic: failed to store assistant response", error=str(exc))

    processing_time_ms = int((time.time() - t_start) * 1000)
    logfire.info(
        "POST /chat success",
        session_id=body.session_id[:8],
        agents=output.agents_called,
        duration_ms=processing_time_ms,
    )

    if body.stream:
        return StreamingResponse(
            _sse_generator(output.response),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return ChatResponse(
        response=output.response,
        session_id=output.session_id,
        memories_used=output.memories_used,
        agents_called=output.agents_called,
        processing_time_ms=processing_time_ms,
        model_used=output.model_used,
        detected_language=output.detected_language,
        warnings=output.warnings,
    )


@router.get("/sessions", summary="List active conversation sessions")
async def list_sessions(request: Request) -> list[dict]:
    """
    List all sessions found in Redis with their message counts.

    API-3 FIX: Reuses app.state.episodic._redis instead of creating a new connection.
    """
    logfire.debug("GET /sessions")
    state = request.app.state
    episodic: EpisodicMemory = state.episodic

    # API-3 FIX: reuse existing Redis connection from episodic memory store
    await episodic._ensure_redis()
    r = episodic._redis

    results: list[dict] = []
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match="session:*:messages", count=100)
        for key in keys:
            session_id = key.split(":")[1]
            count = await r.llen(key)
            results.append({"session_id": session_id, "message_count": count})
        if cursor == 0:
            break
    return results


@router.delete("/sessions/{session_id}", summary="Clear a session's conversation history")
async def delete_session(request: Request, session_id: str) -> dict:
    """Delete all Redis keys associated with a conversation session."""
    logfire.debug("DELETE /sessions/{session_id}", session_id=session_id[:8])
    state = request.app.state
    episodic: EpisodicMemory = state.episodic
    await episodic.clear_session(session_id)
    logfire.info("Session cleared", session_id=session_id[:8])
    return {"deleted": True}


@router.get("/sessions/{session_id}/history", summary="Get session message history")
async def get_session_history(
    request: Request, session_id: str, last_n: int = 50
) -> list[dict]:
    """Retrieve the most recent messages for a session."""
    logfire.debug("GET /sessions/{session_id}/history", session_id=session_id[:8])
    state = request.app.state
    episodic: EpisodicMemory = state.episodic
    return await episodic.get_history(session_id, last_n=last_n)
