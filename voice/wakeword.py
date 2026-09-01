"""
Yaazhi Wake Word Listener — "Hey Yaazhi" detection daemon.

Audit fixes applied (2026-05-10):
  V5 : asyncio.get_event_loop() replaced with get_running_loop() / new_event_loop().
  V6 : Logs a startup warning when custom ONNX model is not found and falls back
       to hey_jarvis built-in model.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import logfire

from config.settings import settings


class WakeWordListener:
    """
    Background daemon that listens for "Hey Yaazhi" wake word.

    Uses openwakeword for detection. On a wake event, fires an async
    callback (typically to start a voice session).

    V5 FIX: Uses asyncio.get_running_loop() with fallback to new_event_loop().
    V6 FIX: Warns explicitly when custom model is absent, falls back to hey_jarvis.
    """

    _DEFAULT_MODEL = "hey_jarvis"
    _SAMPLE_RATE = 16000
    _CHUNK_SIZE = 1280  # ~80ms at 16kHz — openwakeword default
    _COOLDOWN_SECS = 5.0

    def __init__(
        self,
        on_wake: Optional[Callable[[], None]] = None,
        model_path: Optional[str] = None,
    ) -> None:
        """
        Initialise listener.

        Args:
            on_wake   : Sync or async callable fired on wake detection.
            model_path: Path to custom ONNX wake word model. Falls back to
                        hey_jarvis if None or file not found.
        """
        self._on_wake = on_wake
        self._model_path = model_path
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._audio_queue: queue.Queue = queue.Queue()
        self._last_wake_time: float = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

    def __repr__(self) -> str:
        return f"WakeWordListener(running={self._running})"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_model(self) -> str:
        """
        Resolve the model to use, with V6 fallback warning.

        V6 FIX: Explicitly warns and returns _DEFAULT_MODEL when the
        configured custom model file does not exist.
        """
        if self._model_path:
            p = Path(self._model_path)
            if p.exists() and p.suffix.lower() == ".onnx":
                logfire.info("WakeWordListener: using custom model", path=str(p))
                return str(p)
            # V6 FIX: explicit warning, not silent fallback
            logfire.warning(
                "WakeWordListener: custom ONNX model not found — "
                "falling back to built-in 'hey_jarvis' model. "
                "Custom wake word 'Hey Yaazhi' is NOT active.",
                configured_path=self._model_path,
            )
        return self._DEFAULT_MODEL

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """
        V5 FIX: Use get_running_loop() if inside an async context,
        otherwise create a new event loop.
        """
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    # ------------------------------------------------------------------
    # Listener thread
    # ------------------------------------------------------------------

    def _listener_thread(self, model_name: str, sensitivity: float) -> None:
        """
        Main listener loop running in a daemon thread.

        Reads microphone chunks, runs openwakeword inference,
        and fires the callback when confidence exceeds threshold.
        """
        try:
            import pyaudio  # type: ignore
            import numpy as np  # type: ignore
            from openwakeword.model import Model  # type: ignore
        except ImportError as exc:
            logfire.error("WakeWordListener: required package missing", error=str(exc))
            return

        try:
            oww_model = Model(
                wakeword_models=[model_name] if "/" not in model_name else [],
                custom_verifier_models={} if "/" not in model_name else {
                    "hey_yaazhi": model_name
                },
                inference_framework="onnx",
            )
        except Exception as exc:
            logfire.error("WakeWordListener: model load failed", error=str(exc))
            return

        pa = pyaudio.PyAudio()
        stream = None
        try:
            stream = pa.open(
                rate=self._SAMPLE_RATE,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=self._CHUNK_SIZE,
            )
            logfire.info("WakeWordListener: microphone stream opened")

            while not self._stop_event.is_set():
                try:
                    chunk = stream.read(self._CHUNK_SIZE, exception_on_overflow=False)
                except OSError:
                    continue

                audio_array = np.frombuffer(chunk, dtype=np.int16)
                try:
                    prediction = oww_model.predict(audio_array)
                except Exception:
                    continue

                # Check any model's score
                max_score = max(prediction.values(), default=0.0)
                if max_score >= sensitivity:
                    now = time.monotonic()
                    if now - self._last_wake_time < self._COOLDOWN_SECS:
                        continue  # within cooldown window

                    self._last_wake_time = now
                    logfire.info(
                        "WakeWordListener: wake word detected",
                        score=round(float(max_score), 3),
                    )
                    self._fire_callback()

        except Exception as exc:
            logfire.error("WakeWordListener thread error", error=str(exc))
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()

    def _fire_callback(self) -> None:
        """
        Fire the on_wake callback.

        Handles both sync and async callables.
        V5 FIX: uses asyncio.run_coroutine_threadsafe with a properly obtained loop.
        """
        if self._on_wake is None:
            return

        if asyncio.iscoroutinefunction(self._on_wake):
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._on_wake(), self._loop)
            else:
                logfire.warning("WakeWordListener: no running loop to dispatch async callback")
        else:
            try:
                self._on_wake()
            except Exception as exc:
                logfire.error("WakeWordListener: on_wake callback raised", error=str(exc))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Start the listener daemon thread.

        V5 FIX: Stores running loop reference obtained via get_running_loop().
        """
        if self._running:
            logfire.warning("WakeWordListener.start: already running")
            return

        # V5 FIX: correct way to get the running loop
        self._loop = self._get_loop()

        model_name = self._resolve_model()
        sensitivity = settings.wakeword_sensitivity

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._listener_thread,
            args=(model_name, sensitivity),
            daemon=True,
            name="yaazhi-wakeword",
        )
        self._thread.start()
        self._running = True
        logfire.info(
            "WakeWordListener started",
            model=model_name,
            sensitivity=sensitivity,
        )

    async def stop(self) -> None:
        """Stop the listener daemon thread gracefully."""
        if not self._running:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._running = False
        logfire.info("WakeWordListener stopped")

    async def ping(self) -> bool:
        """Return True if the listener thread is alive."""
        logfire.debug("WakeWordListener.ping called")
        return self._running and self._thread is not None and self._thread.is_alive()

    def process_chunk(self, pcm_bytes: bytes, threshold: float | None = None) -> bool:
        """
        Convenience synchronous helper for tests to run prediction on a single audio chunk.

        Converts raw PCM bytes to numpy array and calls openwakeword.Model.predict() if available.
        Returns True when the maximum model score exceeds threshold (or configured sensitivity).
        """
        try:
            import numpy as np  # type: ignore
        except Exception:
            # If numpy isn't installed in the test environment, behave conservatively
            return False

        try:
            from openwakeword.model import Model  # type: ignore
        except Exception:
            return False

        try:
            audio_array = np.frombuffer(pcm_bytes, dtype=np.int16)
            model = Model()
            prediction = model.predict(audio_array)
            max_score = max(prediction.values(), default=0.0)
            th = threshold if threshold is not None else settings.wakeword_sensitivity
            return bool(max_score >= th)
        except Exception:
            return False


WakeWordDetector = WakeWordListener
WakeWordEngine = WakeWordListener
