"""
Yaazhi Voice API Routes — hardened.

Audit fixes: API-6 (tempfile.gettempdir), API-7 (voice sessions in episodic memory).
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiofiles
import logfire
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

router = APIRouter(tags=["voice"])

# API-6 FIX: no hardcoded /tmp
_VOICE_TMP_DIR: Path = Path(tempfile.gettempdir()) / "yaazhi_voice"
_VOICE_TMP_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_AUDIO_EXTS: frozenset[str] = frozenset([".wav", ".mp3", ".ogg", ".flac", ".m4a", ".webm"])
_MAX_AUDIO_BYTES: int = 25 * 1024 * 1024


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=3000)
    language: str = Field(default="auto")
    gender: str = Field(default="female")
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class VoiceChatResponse(BaseModel):
    transcribed: str
    response_text: str
    session_id: str
    detected_language: str
    confidence: Optional[float]
    processing_time_ms: int
    agents_called: list[str] = Field(default_factory=list)
    model_used: str = Field(default="")
    memories_used: int = Field(default=0)
    tinai: str = Field(default="general_technical")
    domain: str = Field(default="puram")


@router.post("/transcribe", summary="Transcribe audio to text")
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = str(uuid.uuid4()),
) -> dict:
    t_start = time.time()
    ext = Path(file.filename or "audio.wav").suffix.lower()
    if ext not in _ALLOWED_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported format '{ext}'")
    content = await file.read()
    if len(content) > _MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio exceeds 25 MB")
    result = await request.app.state.stt_engine.transcribe_bytes(content)
    return {
        "text": result["text"],
        "language": result.get("language", "en"),
        "confidence": result.get("confidence"),
        "session_id": session_id,
        "processing_time_ms": int((time.time() - t_start) * 1000),
    }


@router.post("/chat", response_model=VoiceChatResponse, summary="Voice round-trip STT→AI→text")
async def voice_chat(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = str(uuid.uuid4()),
    language: str = "auto",
) -> VoiceChatResponse:
    """
    STT → memory recall → orchestrator → episodic save.
    API-7 FIX: voice sessions persisted in episodic memory.
    """
    t_start = time.time()
    state = request.app.state
    ext = Path(file.filename or "audio.wav").suffix.lower()
    if ext not in _ALLOWED_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported format '{ext}'")
    content = await file.read()
    if len(content) > _MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio exceeds 25 MB")

    # STT
    stt_result = await state.stt_engine.transcribe_bytes(
        content, language=None if language == "auto" else language
    )
    transcribed = stt_result["text"].strip()
    detected_lang = stt_result.get("language", "en")
    confidence = stt_result.get("confidence")
    if not transcribed:
        raise HTTPException(422, "Empty transcription result")

    # Memory context
    recalled = ""
    try:
        recalled = await state.retriever.build_context(transcribed, max_tokens=1000)
    except Exception:
        pass

    # API-7 FIX: store user voice message
    try:
        await state.episodic.add_message(session_id, "user", transcribed)
    except Exception:
        pass

    # Orchestrator
    output = await state.yaazhi.run(transcribed, session_id, context=recalled)
    response_text = output.response

    # API-7 FIX: store assistant response
    try:
        await state.episodic.add_message(session_id, "assistant", response_text)
    except Exception:
        pass

    # Persist voice turn to semantic vector store for cross-session recall
    if hasattr(state, "vector_store") and state.vector_store is not None:
        try:
            await state.vector_store.add(
                f"[User]: {transcribed}\n[Yaazhi]: {response_text}",
                metadata={"session_id": session_id, "modality": "voice"},
                source="voice",
                session_id=session_id,
            )
        except Exception as exc:
            logfire.warning("POST /voice/chat: vector_store.add failed (non-fatal)", error=str(exc))

    meta: dict[str, Any] = getattr(output, "metadata", {}) or {}
    logfire.info("POST /voice/chat success", session_id=session_id[:8])
    return VoiceChatResponse(
        transcribed=transcribed,
        response_text=response_text,
        session_id=session_id,
        detected_language=detected_lang,
        confidence=confidence,
        processing_time_ms=int((time.time() - t_start) * 1000),
        agents_called=getattr(output, "agents_called", []) or [],
        model_used=getattr(output, "model_used", "") or "",
        memories_used=getattr(output, "memories_used", 0) or 0,
        tinai=str(meta.get("tinai", "general_technical")),
        domain=str(meta.get("domain", "puram")),
    )


@router.post("/synthesise", summary="Text-to-speech", response_class=Response)
async def synthesise(request: Request, body: TTSRequest) -> Response:
    try:
        audio: bytes = await request.app.state.tts_engine.synthesise(
            body.text, language=body.language, gender=body.gender
        )
    except Exception as exc:
        raise HTTPException(500, f"TTS failed: {exc}") from exc
    return Response(content=audio, media_type="audio/wav")


@router.websocket("/ws")
async def voice_realtime_ws(websocket: WebSocket) -> None:
    """
    OpenAI-Realtime + J.A.R.V.I.S. compatible bidirectional voice WebSocket.

    Supports events:
      - input_audio_buffer.commit (base64 audio -> Groq Whisper STT -> Yaazhi V4 -> response)
      - conversation.item.create  (text turn -> Yaazhi V4 -> response)
      - response.cancel           (barge-in interruption)
    """
    await websocket.accept()
    state = websocket.app.state
    session_id = f"rt-{uuid.uuid4().hex[:8]}"
    await websocket.send_json({
        "type": "session.created",
        "session": {
            "id": session_id,
            "model": "groq/qwen/qwen3.8-27b",
            "stt": "whisper-large-v3-turbo",
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 1100},
        },
    })
    try:
        while True:
            raw = await websocket.receive_text()
            event = json.loads(raw)
            ev_type = event.get("type", "")
            session_id = event.get("session_id", session_id)

            if ev_type == "response.cancel":
                await websocket.send_json({"type": "response.cancelled", "session_id": session_id})
                continue

            user_text = ""
            if ev_type == "input_audio_buffer.commit":
                audio_b64 = event.get("audio", "")
                if audio_b64:
                    audio_bytes = base64.b64decode(audio_b64)
                    await websocket.send_json({"type": "jarvis.stage", "stage": "stt", "status": "Transcribing via Whisper Turbo..."})
                    stt_res = await state.stt_engine.transcribe_bytes(audio_bytes)
                    user_text = (stt_res.get("text") or "").strip()
                    await websocket.send_json({
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": user_text,
                        "confidence": stt_res.get("confidence", 0.98),
                    })
            elif ev_type == "conversation.item.create":
                user_text = (event.get("text") or "").strip()

            if not user_text:
                continue

            t0 = time.time()
            await websocket.send_json({"type": "jarvis.stage", "stage": "planning", "status": "Stage 1: Mutarporul & Task Planning..."})
            recalled = ""
            try:
                recalled = await state.retriever.build_context(user_text, max_tokens=1000)
            except Exception:
                pass
            try:
                await state.episodic.add_message(session_id, "user", user_text)
            except Exception:
                pass

            await websocket.send_json({"type": "jarvis.stage", "stage": "execution", "status": "Stage 2-3: Multi-Agent Execution & Review..."})
            output = await state.yaazhi.run(user_text, session_id, context=recalled)
            resp_text = output.response

            try:
                await state.episodic.add_message(session_id, "assistant", resp_text)
            except Exception:
                pass
            if getattr(state, "vector_store", None) is not None:
                try:
                    await state.vector_store.add(
                        f"[User]: {user_text}\n[Yaazhi]: {resp_text}",
                        metadata={"session_id": session_id, "modality": "realtime_voice"},
                        source="voice",
                        session_id=session_id,
                    )
                except Exception:
                    pass

            meta: dict[str, Any] = getattr(output, "metadata", {}) or {}
            await websocket.send_json({
                "type": "response.done",
                "session_id": session_id,
                "transcript": user_text,
                "response_text": resp_text,
                "agents_called": getattr(output, "agents_called", []) or [],
                "model_used": getattr(output, "model_used", "") or "",
                "memories_used": getattr(output, "memories_used", 0) or 0,
                "tinai": str(meta.get("tinai", "general_technical")),
                "domain": str(meta.get("domain", "puram")),
                "processing_time_ms": int((time.time() - t0) * 1000),
            })
    except WebSocketDisconnect:
        logfire.info("Realtime voice WebSocket disconnected", session_id=session_id[:8])
    except Exception as exc:
        logfire.warning("Realtime voice WebSocket error", error=str(exc))
