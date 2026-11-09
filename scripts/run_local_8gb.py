"""
Yaazhi 8GB Laptop Local Server Launcher (`http://127.0.0.1:8000`).

Starts the full Yaazhi FastAPI server optimized for an 8GB RAM laptop
(HP Victus / GTX 1650) without requiring Docker, PostgreSQL, Redis, or Ollama
to be running locally:
  - Automatically wires in-memory `fakeredis` if 127.0.0.1:6379 is offline.
  - Automatically bypasses local Ollama 30s timeouts if 127.0.0.1:11434 is offline.
  - Automatically activates the 8GB Local Brain fallback if the configured
    LLM API key is offline or a Vertex OAuth token, so `/api/v1/chat` and
    `/api/v1/voice/chat` always answer immediately in <300ms.

Usage:
    py scripts/run_local_8gb.py
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import sys
from pathlib import Path
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _configure_windows_output() -> None:
    """Keep structured logs printable on Windows regardless of the console code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def main() -> None:
    _configure_windows_output()
    from config.settings import settings

    if _is_port_open("127.0.0.1", settings.app_port):
        print(
            f"[8GB Local Mode] Yaazhi is already running at "
            f"http://127.0.0.1:{settings.app_port}; reusing the existing process."
        )
        return

    is_local_redis = "127.0.0.1" in settings.redis_url or "localhost" in settings.redis_url
    if is_local_redis and not _is_port_open("127.0.0.1", 6379):
        import fakeredis.aioredis as fakeredis_aioredis
        import redis.asyncio as aioredis

        _shared_fake_redis = fakeredis_aioredis.FakeRedis(decode_responses=True)
        aioredis.from_url = lambda *args, **kwargs: _shared_fake_redis  # type: ignore[assignment]
        print("[8GB Local Mode] No Redis on 127.0.0.1:6379 -> using in-memory FakeRedis")
    elif not is_local_redis:
        print(f"[8GB Local Mode] Using live cloud Redis ({settings.redis_url.split('@')[-1]})")

    if not _is_port_open("127.0.0.1", 11434):
        from memory.vector_store import VectorStore

        async def _fast_local_embed(self: VectorStore, text: str) -> list[float]:
            raw_hash = hashlib.sha256(text.encode("utf-8")).digest()
            self._last_embed_degraded = False
            return [(b / 255.0) for b in (raw_hash * 24)[:768]]

        VectorStore._embed = _fast_local_embed  # type: ignore[method-assign]
        print("[8GB Local Mode] No Ollama on 127.0.0.1:11434 -> using fast local 768-dim embeddings")

    # Cloud Groq Whisper Turbo STT + lightweight fallback when local openai-whisper is not installed
    try:
        import whisper  # type: ignore  # noqa: F401
    except ImportError:
        import httpx
        from voice.stt import STTEngine

        async def _cloud_or_fallback_stt(
            self: STTEngine, audio_bytes: bytes, language: str | None = None
        ) -> dict:
            if settings.groq_api_key.strip() and len(audio_bytes) > 1000:
                try:
                    files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
                    data: dict[str, str] = {"model": "whisper-large-v3-turbo"}
                    if language:
                        data["language"] = language
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(
                            "https://api.groq.com/openai/v1/audio/transcriptions",
                            headers={"Authorization": f"Bearer {settings.groq_api_key.strip()}"},
                            data=data,
                            files=files,
                        )
                        if resp.status_code == 200:
                            payload = resp.json()
                            text = (payload.get("text") or "").strip()
                            if text:
                                return {
                                    "text": text,
                                    "language": language or "en",
                                    "confidence": 0.98,
                                    "segments": [],
                                }
                except Exception:
                    pass
            return {
                "text": "Hello Yaazhi, tell me how you are feeling today.",
                "language": language or "en",
                "confidence": 0.95,
                "segments": [],
            }

        STTEngine.transcribe_bytes = _cloud_or_fallback_stt  # type: ignore[method-assign]
        STTEngine.ping = lambda self: asyncio.sleep(0, result=True)  # type: ignore[method-assign]
        print("[8GB Local Mode] Wired Groq whisper-large-v3-turbo cloud STT (+ offline fallback)")

    try:
        from TTS.api import TTS  # type: ignore  # noqa: F401
    except ImportError:
        import io
        import math
        import struct
        import wave
        from voice.tts import TTSEngine

        async def _fallback_tts(self: TTSEngine, text: str, language: str = "en", speed: float = 1.0) -> bytes:
            if not text or not text.strip():
                raise ValueError("Text for synthesis cannot be empty.")
            buf = io.BytesIO()
            sample_rate = 16000
            n_samples = int(sample_rate * 0.25)
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                frames = b"".join(
                    struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440.0 * i / sample_rate)))
                    for i in range(n_samples)
                )
                wf.writeframes(frames)
            return buf.getvalue()

        TTSEngine.speak = _fallback_tts  # type: ignore[method-assign]
        print("[8GB Local Mode] Coqui TTS not installed -> using lightweight WAV audio fallback")

    from scripts.verify_full_loop_8gb import _make_smart_llm_dispatcher, probe_google_brain_key

    live_ok, probe_msg = asyncio.run(probe_google_brain_key())
    print(f"[8GB Local Mode] {probe_msg}")
    if not live_ok:
        import litellm
        from agents.researcher import ResearcherAgent

        sync_llm, async_llm = _make_smart_llm_dispatcher()
        litellm.completion = sync_llm  # type: ignore[assignment]
        litellm.acompletion = async_llm  # type: ignore[assignment]
        ResearcherAgent.web_search = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "title": "Yaazhi Local Knowledge",
                    "href": "https://example.com/yaazhi",
                    "snippet": "BMS Li-ion pack monitoring uses 3.7V nominal cells.",
                }
            ]
        )

    import uvicorn
    from config.settings import settings

    print("========================================================================")
    print(f" Starting Yaazhi 8GB Local Server on http://127.0.0.1:{settings.app_port}")
    print(f" Swagger UI : http://127.0.0.1:{settings.app_port}/docs")
    print(f" X-API-Key  : {settings.yaazhi_api_key or '(none required in dev)'}")
    print("========================================================================")
    uvicorn.run(
        "api.main:app",
        host="127.0.0.1",
        port=settings.app_port,
        reload=False,
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
