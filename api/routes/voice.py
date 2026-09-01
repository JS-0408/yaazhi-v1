"""
Yaazhi Voice API Routes — hardened.

Audit fixes: API-6 (tempfile.gettempdir), API-7 (voice sessions in episodic memory).
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
import logfire
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
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

    logfire.info("POST /voice/chat success", session_id=session_id[:8])
    return VoiceChatResponse(
        transcribed=transcribed,
        response_text=response_text,
        session_id=session_id,
        detected_language=detected_lang,
        confidence=confidence,
        processing_time_ms=int((time.time() - t_start) * 1000),
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
