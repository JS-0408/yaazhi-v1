"""
Yaazhi STT Engine — Whisper wrapper.

Audit fixes applied (2026-05-10):
  V1 : Real confidence computed from Whisper segment avg_logprob.
       No more hardcoded confidence = 0.9.
  V2 : Temp file writes use aiofiles (non-blocking).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiofiles
import logfire


class STTEngine:
    """
    Speech-to-text engine wrapping OpenAI Whisper.

    Model is loaded lazily on first transcription call to avoid
    blocking FastAPI startup for 30–120 seconds.

    V1 FIX: Confidence score derived from Whisper segment avg_logprob.
    V2 FIX: Temp file written asynchronously via aiofiles.
    """

    def __init__(self) -> None:
        self._model = None
        self._model_size: str = "base"
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        loaded = self._model is not None
        return f"STTEngine(model_size={self._model_size!r}, loaded={loaded})"

    async def ping(self) -> bool:
        """Verify whisper can be imported (does NOT load model — stays fast)."""
        logfire.debug("STTEngine.ping called")
        try:
            import whisper  # type: ignore  # noqa: F401
            logfire.info("STTEngine.ping success")
            return True
        except ImportError as exc:
            logfire.error("STTEngine.ping failed — whisper not installed", error=str(exc))
            return False

    async def _ensure_model(self) -> None:
        """Lazy-load the Whisper model (once, thread-safe)."""
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            logfire.info("STTEngine: loading Whisper model", size=self._model_size)
            t_start = time.time()
            import whisper  # type: ignore
            from config.settings import settings
            self._model_size = settings.whisper_model_size
            self._model = await asyncio.to_thread(
                whisper.load_model,
                self._model_size,
                device="cpu",
                in_memory=False,
            )
            duration_ms = int((time.time() - t_start) * 1000)
            logfire.info("STTEngine: Whisper model loaded", duration_ms=duration_ms)

    @staticmethod
    def _compute_confidence(result: dict) -> Optional[float]:
        """
        V1 FIX: Compute real transcription confidence from segment avg_logprob.

        Whisper's avg_logprob is negative (0 = perfect). Convert to 0–1:
            confidence = clamp(1.0 + avg_logprob / 10.0, 0.0, 1.0)

        Returns None if no segments are available (do not fake a value).
        """
        segments = result.get("segments", [])
        if not segments:
            return None
        avg_lp = sum(s.get("avg_logprob", -1.0) for s in segments) / len(segments)
        confidence = float(min(1.0, max(0.0, 1.0 + avg_lp / 10.0)))
        return round(confidence, 4)

    async def transcribe_bytes(
        self,
        audio_bytes: bytes,
        language: Optional[str] = None,
    ) -> dict:
        """
        Transcribe raw audio bytes using Whisper.

        V2 FIX: Temp file written with aiofiles (non-blocking I/O).

        Args:
            audio_bytes : Raw audio bytes (WAV, MP3, FLAC, OGG, M4A).
            language    : ISO language hint (None = auto-detect).

        Returns:
            Dict with:
              - text: str  (transcribed text)
              - language: str (detected language code)
              - confidence: float | None (derived from avg_logprob, or None)
              - segments: list[dict] (raw Whisper segments)
        """
        logfire.debug("STTEngine.transcribe_bytes", bytes=len(audio_bytes))
        t_start = time.time()
        await self._ensure_model()

        # V2 FIX: write temp file asynchronously
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="yaazhi_stt_")
            os.close(fd)
            async with aiofiles.open(tmp_path, "wb") as fh:
                await fh.write(audio_bytes)

            transcribe_kwargs: dict = {
                "fp16": False,       # CPU-safe for ARM64
                "verbose": False,
            }
            if language:
                transcribe_kwargs["language"] = language

            result: dict = await asyncio.to_thread(
                self._model.transcribe, tmp_path, **transcribe_kwargs
            )

            text: str = result.get("text", "").strip()
            detected_lang: str = result.get("language", language or "en")
            confidence = self._compute_confidence(result)   # V1 FIX

            duration_ms = int((time.time() - t_start) * 1000)
            logfire.info(
                "STTEngine.transcribe_bytes success",
                chars=len(text),
                language=detected_lang,
                confidence=confidence,
                duration_ms=duration_ms,
            )

            return {
                "text": text,
                "language": detected_lang,
                "confidence": confidence,    # V1 FIX: real value or None
                "segments": result.get("segments", []),
            }

        except Exception as exc:
            logfire.error("STTEngine.transcribe_bytes failed", error=str(exc))
            raise RuntimeError(f"Transcription failed: {exc}") from exc
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    async def transcribe_file(self, file_path: str, language: Optional[str] = None) -> dict:
        """
        Transcribe an audio file from disk path.

        Reads the file asynchronously before passing bytes to transcribe_bytes.
        """
        logfire.debug("STTEngine.transcribe_file", path=file_path)
        async with aiofiles.open(file_path, "rb") as fh:
            audio_bytes = await fh.read()
        return await self.transcribe_bytes(audio_bytes, language=language)
