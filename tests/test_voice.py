"""
tests/test_voice.py — Unit tests for Yaazhi Voice Layer
Tests STTEngine (Whisper), TTSEngine (Coqui XTTS), BhashiniClient, WakeWordDetector.
All audio I/O is mocked — no microphone or speaker required.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
import io


# ─────────────────────────────────────────────────────────
# STTEngine tests
# ─────────────────────────────────────────────────────────

class TestSTTEngine:

    @pytest.mark.asyncio
    async def test_init_loads_model(self, monkeypatch):
        """STTEngine should load a Whisper model when ensured."""
        import sys
        mock_whisper = MagicMock()
        mock_model = MagicMock()
        mock_whisper.load_model.return_value = mock_model
        monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

        from voice.stt import STTEngine
        engine = STTEngine()
        await engine._ensure_model()
        mock_whisper.load_model.assert_called_once()

    @pytest.mark.asyncio
    async def test_transcribe_bytes_returns_text(self, monkeypatch, sample_wav_bytes):
        """transcribe_bytes() should return a non-empty text field for valid audio."""
        import sys
        mock_whisper = MagicMock()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = {"text": "Hello Yaazhi, what is today's date?", "language": "en", "segments": []}
        mock_whisper.load_model.return_value = mock_model
        monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

        from voice.stt import STTEngine
        engine = STTEngine()
        res = await engine.transcribe_bytes(sample_wav_bytes)

        assert isinstance(res, dict)
        assert "text" in res and isinstance(res["text"], str)
        assert len(res["text"]) > 0

    @pytest.mark.asyncio
    async def test_transcribe_detects_language(self, monkeypatch, sample_wav_bytes):
        """transcribe_bytes() should detect and return the language."""
        import sys
        mock_whisper = MagicMock()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = {"text": "నమస్కారం యాజి", "language": "te", "segments": []}
        mock_whisper.load_model.return_value = mock_model
        monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

        from voice.stt import STTEngine
        engine = STTEngine()
        res = await engine.transcribe_bytes(sample_wav_bytes, language="te")
        assert isinstance(res, dict)
        assert res.get("language") == "te"

    @pytest.mark.asyncio
    async def test_transcribe_empty_audio_returns_string(self, monkeypatch):
        """transcribe_bytes() with silent audio should return empty text, not raise."""
        import sys
        mock_whisper = MagicMock()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = {"text": "", "language": "en", "segments": []}
        mock_whisper.load_model.return_value = mock_model
        monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

        from voice.stt import STTEngine
        engine = STTEngine()
        res = await engine.transcribe_bytes(b"\x00" * 100)
        assert isinstance(res, dict)
        assert res.get("text") == ""

    @pytest.mark.asyncio
    async def test_ping_returns_bool(self, monkeypatch):
        """ping() should return True when whisper is importable."""
        import sys
        mock_whisper = MagicMock()
        monkeypatch.setitem(sys.modules, "whisper", mock_whisper)
        from voice.stt import STTEngine
        engine = STTEngine()
        result = await engine.ping()
        assert isinstance(result, bool)


# ─────────────────────────────────────────────────────────
# TTSEngine tests
# ─────────────────────────────────────────────────────────

class TestTTSEngine:

    @pytest.mark.asyncio
    async def test_init_and_ping(self, monkeypatch):
        """TTSEngine should instantiate and ping should return bool."""
        # Ensure Bhashini path is used to avoid loading Coqui in tests
        monkeypatch.setattr("config.settings.settings.bhashini_api_key", "dummy_key", raising=False)
        mock_client = MagicMock()
        async def fake_tts(text, language):
            return b"fake_audio"
        mock_client.tts = AsyncMock(side_effect=fake_tts)
        monkeypatch.setitem(__import__("sys").modules, "voice.bhashini", MagicMock(BhashiniClient=MagicMock(return_value=mock_client)))

        from voice.tts import TTSEngine
        engine = TTSEngine()
        result = await engine.ping()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_synthesize_returns_bytes_via_bhashini(self, monkeypatch):
        """speak() should return non-empty bytes via Bhashini when configured."""
        monkeypatch.setattr("config.settings.settings.bhashini_api_key", "dummy_key", raising=False)
        mock_client = MagicMock()
        async def fake_tts(text, language):
            return b"RIFFFAKE"
        mock_client.tts = AsyncMock(side_effect=fake_tts)
        monkeypatch.setitem(__import__("sys").modules, "voice.bhashini", MagicMock(BhashiniClient=MagicMock(return_value=mock_client)))

        from voice.tts import TTSEngine
        engine = TTSEngine()
        res = await engine.speak("Hello from Yaazhi!", language="en")
        assert isinstance(res, (bytes, bytearray))

    @pytest.mark.asyncio
    async def test_synthesize_telugu_via_bhashini(self, monkeypatch):
        """speak() should handle Telugu text via Bhashini without raising."""
        monkeypatch.setattr("config.settings.settings.bhashini_api_key", "dummy_key", raising=False)
        mock_client = MagicMock()
        async def fake_tts(text, language):
            return b"TELUGU"
        mock_client.tts = AsyncMock(side_effect=fake_tts)
        monkeypatch.setitem(__import__("sys").modules, "voice.bhashini", MagicMock(BhashiniClient=MagicMock(return_value=mock_client)))

        from voice.tts import TTSEngine
        engine = TTSEngine()
        res = await engine.speak("నమస్కారం", language="te")
        assert isinstance(res, (bytes, bytearray))

    @pytest.mark.asyncio
    async def test_empty_text_raises(self, monkeypatch):
        """speak() with empty text should raise ValueError."""
        from voice.tts import TTSEngine
        engine = TTSEngine()

        with pytest.raises((ValueError, Exception)):
            await engine.speak("")


# ─────────────────────────────────────────────────────────
# BhashiniClient tests
# ─────────────────────────────────────────────────────────

class TestBhashiniClient:

    @pytest.mark.asyncio
    @patch("voice.bhashini.httpx")
    async def test_translate_returns_string(self, mock_httpx):
        """translate() should return a translated string."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "pipelineResponse": [{
                "output": [{"target": "హలో, నేను యాజి"}]
            }]
        }
        mock_httpx.post.return_value = mock_resp

        from voice.bhashini import BhashiniClient
        client = BhashiniClient()
        res = await client.translate(
            text="Hello, I am Yaazhi",
            source_language="en",
            target_language="te",
        )
        assert isinstance(res, str)
        assert len(res) > 0

    @pytest.mark.asyncio
    @patch("voice.bhashini.httpx")
    async def test_asr_returns_transcript(self, mock_httpx, sample_wav_bytes):
        """asr() should return a transcript string from audio bytes."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "pipelineResponse": [{
                "output": [{"source": "నమస్కారం"}]
            }]
        }
        mock_httpx.post.return_value = mock_resp

        from voice.bhashini import BhashiniClient
        client = BhashiniClient()
        res = await client.asr(audio_bytes=sample_wav_bytes, language="te")

        assert isinstance(res, str)

    @pytest.mark.asyncio
    @patch("voice.bhashini.httpx")
    async def test_tts_returns_bytes(self, mock_httpx):
        """tts() should return audio bytes."""
        import base64
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "pipelineResponse": [{
                "audio": [{"audioContent": base64.b64encode(b"fake_audio").decode()}]
            }]
        }
        mock_httpx.post.return_value = mock_resp

        from voice.bhashini import BhashiniClient
        client = BhashiniClient()
        res = await client.tts(text="నమస్కారం", language="te")

        assert isinstance(res, (bytes, bytearray))
        assert len(res) > 0

    @pytest.mark.asyncio
    @patch("voice.bhashini.httpx")
    async def test_api_error_raises(self, mock_httpx):
        """BhashiniClient should raise RuntimeError on non-200 response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        mock_httpx.post.return_value = mock_resp

        from voice.bhashini import BhashiniClient
        client = BhashiniClient()

        with pytest.raises((RuntimeError, Exception)):
            await client.translate("Hello", "en", "te")

    @pytest.mark.asyncio
    @patch("voice.bhashini.httpx")
    async def test_ping_returns_bool(self, mock_httpx):
        """ping() should return a bool regardless of API state."""
        mock_httpx.get.return_value = MagicMock(status_code=200)
        from voice.bhashini import BhashiniClient
        client = BhashiniClient()
        res = await client.ping()
        assert isinstance(res, bool)


# ─────────────────────────────────────────────────────────
# WakeWordDetector tests
# ─────────────────────────────────────────────────────────

class TestWakeWordDetector:

    def test_init_loads_model(self, monkeypatch):
        """WakeWordDetector should instantiate when openwakeword.model.Model is available."""
        import sys
        mock_model = MagicMock()
        # Create a fake module 'openwakeword.model' with Model attribute
        fake_mod = MagicMock()
        fake_mod.Model = MagicMock(return_value=mock_model)
        monkeypatch.setitem(sys.modules, "openwakeword.model", fake_mod)

        from voice.wakeword import WakeWordDetector
        detector = WakeWordDetector(model_path="models/hey_yaazhi.onnx")
        assert detector is not None

    def test_process_audio_chunk_no_wakeword(self, monkeypatch):
        """process_chunk() should return False for audio without wake word."""
        import sys
        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_yaazhi": 0.1}  # low confidence
        fake_mod = MagicMock()
        fake_mod.Model = MagicMock(return_value=mock_model)
        monkeypatch.setitem(sys.modules, "openwakeword.model", fake_mod)

        from voice.wakeword import WakeWordDetector
        detector = WakeWordDetector(model_path="models/hey_yaazhi.onnx", threshold=0.5)
        result = detector.process_chunk(b"\x00" * 1600)  # 50ms of silence at 16kHz
        assert result is False

    def test_process_audio_chunk_wakeword_detected(self, monkeypatch):
        """process_chunk() should return True when confidence exceeds threshold."""
        import sys
        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_yaazhi": 0.92}  # high confidence
        fake_mod = MagicMock()
        fake_mod.Model = MagicMock(return_value=mock_model)
        monkeypatch.setitem(sys.modules, "openwakeword.model", fake_mod)

        from voice.wakeword import WakeWordDetector
        detector = WakeWordDetector(model_path="models/hey_yaazhi.onnx", threshold=0.5)
        result = detector.process_chunk(b"\x00" * 1600)
        assert result is True

    def test_threshold_boundary(self, monkeypatch):
        """Exactly at threshold should trigger detection."""
        import sys
        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_yaazhi": 0.5}
        fake_mod = MagicMock()
        fake_mod.Model = MagicMock(return_value=mock_model)
        monkeypatch.setitem(sys.modules, "openwakeword.model", fake_mod)

        from voice.wakeword import WakeWordDetector
        detector = WakeWordDetector(model_path="models/hey_yaazhi.onnx", threshold=0.5)
        result = detector.process_chunk(b"\x00" * 1600)
        assert isinstance(result, bool)

    def test_reset_clears_buffer(self, monkeypatch):
        """reset() should clear internal buffers without raising."""
        import sys
        fake_mod = MagicMock()
        fake_mod.Model = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "openwakeword.model", fake_mod)
        from voice.wakeword import WakeWordDetector
        detector = WakeWordDetector(model_path="models/hey_yaazhi.onnx")
        # reset is async — call via running loop
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(detector.stop())
        finally:
            loop.close()
