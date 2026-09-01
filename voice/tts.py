"""
Yaazhi Text-to-Speech Engine.

Routing:
  Indian languages (te/hi/ta/kn/mr/bn/gu/pa) + Bhashini key configured → Bhashini API
  All other languages → Coqui XTTS v2 (offline, multilingual)

Both models are lazy-loaded. A lock prevents concurrent Coqui synthesis.
All temp files are cleaned up in finally blocks.

Usage:
    from voice.tts import TTSEngine
    engine = TTSEngine()
    wav_bytes = await engine.speak("Hello world", language="en")
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import logfire

from config.settings import settings

_VOICE_TMP_DIR = "/tmp/yaazhi_voice"

_INDIAN_LANGS = frozenset(["te", "hi", "ta", "kn", "mr", "bn", "gu", "pa"])


class TTSEngine:
    """
    Text-to-speech engine routing between Bhashini and Coqui XTTS v2.

    Coqui model is lazy-loaded and protected by asyncio.Lock.
    Bhashini is used for Indian languages when an API key is configured.
    Falls back to Coqui if Bhashini fails.
    """

    def __init__(self) -> None:
        """
        Initialise TTSEngine with lazy model references and a synthesis lock.
        """
        self._coqui_model: Optional[Any] = None
        self._lock = asyncio.Lock()
        Path(_VOICE_TMP_DIR).mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        """Return concise string representation."""
        coqui_loaded = self._coqui_model is not None
        return f"TTSEngine(coqui_loaded={coqui_loaded})"

    async def ping(self) -> bool:
        """
        TTS models are lazy-loaded; always return True.

        Returns:
            True (TTS availability is checked at synthesis time).
        """
        return True

    def _init_coqui(self) -> None:
        """
        Load the Coqui XTTS v2 multilingual model synchronously.

        Called via asyncio.to_thread to avoid blocking the event loop.
        """
        from TTS.api import TTS  # type: ignore
        logfire.info("Loading Coqui XTTS v2 model")
        self._coqui_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
        logfire.info("Coqui XTTS v2 model loaded")

    async def _load_coqui(self) -> None:
        """
        Ensure Coqui model is loaded, loading it if necessary.
        """
        if self._coqui_model is None:
            await asyncio.to_thread(self._init_coqui)

    async def speak(
        self, text: str, language: str = "en", speed: float = 1.0
    ) -> bytes:
        """
        Convert text to speech and return raw WAV bytes.

        Routes Indian languages to Bhashini when configured,
        falls back to Coqui XTTS on any Bhashini failure.

        Args:
            text: The text to synthesise.
            language: ISO language code (e.g. 'en', 'te', 'hi').
            speed: Synthesis speed multiplier (0.5–2.0).

        Returns:
            Raw WAV audio bytes.

        Raises:
            RuntimeError: If synthesis fails in both Bhashini and Coqui.
        """
        logfire.debug("TTSEngine.speak", language=language, chars=len(text))
        t_start = time.time()

        async with self._lock:
            if language in _INDIAN_LANGS and settings.bhashini_api_key:
                try:
                    from voice.bhashini import BhashiniClient
                    client = BhashiniClient()
                    audio_bytes = await client.tts(text, language)
                    duration_ms = int((time.time() - t_start) * 1000)
                    logfire.info(
                        "TTSEngine.speak success via Bhashini",
                        language=language,
                        bytes=len(audio_bytes),
                        duration_ms=duration_ms,
                    )
                    return audio_bytes
                except Exception as exc:
                    logfire.warning(
                        "Bhashini TTS failed, falling back to Coqui",
                        language=language,
                        error=str(exc),
                    )

            await self._load_coqui()
            tmp_path = os.path.join(_VOICE_TMP_DIR, f"{uuid.uuid4()}.wav")
            try:
                await asyncio.to_thread(
                    self._coqui_model.tts_to_file,
                    text,
                    file_path=tmp_path,
                    language=language if language != "auto" else "en",
                    split_sentences=True,
                )
                with open(tmp_path, "rb") as fh:
                    audio_bytes = fh.read()
            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError as exc:
                    logfire.warning("TTS temp file cleanup failed", path=tmp_path, error=str(exc))

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "TTSEngine.speak success via Coqui",
            language=language,
            bytes=len(audio_bytes),
            duration_ms=duration_ms,
        )
        return audio_bytes

    async def speak_to_file(
        self, text: str, output_path: str, language: str = "en"
    ) -> None:
        """
        Synthesise text and save WAV audio to a file.

        Args:
            text: The text to synthesise.
            output_path: Absolute path to the output WAV file.
            language: ISO language code.
        """
        logfire.debug("TTSEngine.speak_to_file", output_path=output_path)
        audio_bytes = await self.speak(text, language)
        with open(output_path, "wb") as fh:
            fh.write(audio_bytes)
        logfire.info("TTSEngine.speak_to_file success", path=output_path, bytes=len(audio_bytes))

    async def get_supported_languages(self) -> list[dict]:
        """
        Return the list of languages supported by this TTS engine.

        Returns:
            List of dicts with code, name, engine, and sample_text.
        """
        return [
            {
                "code": "en",
                "name": "English",
                "engine": "coqui-xtts-v2",
                "sample_text": "Hello, I am Yaazhi.",
            },
            {
                "code": "te",
                "name": "Telugu",
                "engine": "bhashini",
                "sample_text": "నమస్కారం, నేను యాజి.",
            },
            {
                "code": "hi",
                "name": "Hindi",
                "engine": "bhashini",
                "sample_text": "नमस्ते, मैं याज़ी हूं।",
            },
            {
                "code": "ta",
                "name": "Tamil",
                "engine": "bhashini",
                "sample_text": "வணக்கம், நான் யாழி.",
            },
            {
                "code": "kn",
                "name": "Kannada",
                "engine": "bhashini",
                "sample_text": "ನಮಸ್ಕಾರ, ನಾನು ಯಾಳಿ.",
            },
        ]
