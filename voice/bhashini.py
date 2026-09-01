"""
Yaazhi Bhashini API Client.

Wraps the Indian government's ULCA Bhashini NLP pipeline for:
  - Translation (source → target language)
  - Text-to-speech (Indian languages)
  - Automatic speech recognition (Indian languages)
  - Language detection via Unicode range analysis

Pipeline configs are cached in memory to avoid repeated API calls.

Usage:
    from voice.bhashini import BhashiniClient
    client = BhashiniClient()
    text = await client.translate("Hello", "en", "te")
    audio = await client.tts("నమస్కారం", "te")
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Optional

import httpx
import logfire

from config.settings import settings


class BhashiniClient:
    """
    Client for the Indian government Bhashini ULCA language services.

    Supports translation, TTS, ASR, and language detection.
    Pipeline configurations are cached per (task_type, source_lang, target_lang) key.
    All HTTP errors are logged and raised as RuntimeError for callers to handle.
    """

    def __init__(self) -> None:
        """
        Initialise Bhashini client with API credentials from settings.

        All credentials are read from settings — never hardcoded.
        """
        self._api_key: str = settings.bhashini_api_key
        self._user_id: str = settings.bhashini_user_id
        self._pipeline_url: str = settings.bhashini_pipeline_url
        self._pipeline_cache: dict[str, dict] = {}
        self._http = httpx.AsyncClient(
            headers={
                "userID": self._user_id,
                "ulcaApiKey": self._api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def __repr__(self) -> str:
        """Return concise string representation."""
        has_key = bool(self._api_key)
        return f"BhashiniClient(has_key={has_key}, user_id={self._user_id!r})"

    async def ping(self) -> bool:
        """
        Check connectivity to the Bhashini pipeline endpoint.

        Returns:
            True if the endpoint returns HTTP status < 500.
        """
        logfire.debug("BhashiniClient.ping called")
        try:
            resp = await self._http.get(self._pipeline_url, timeout=10.0)
            ok = resp.status_code < 500
            logfire.info("BhashiniClient.ping", status=resp.status_code, ok=ok)
            return ok
        except Exception as exc:
            logfire.error("BhashiniClient.ping failed", error=str(exc))
            return False

    async def _get_pipeline(
        self, source_lang: str, task_type: str, target_lang: str = ""
    ) -> dict:
        """
        Fetch and cache a Bhashini pipeline configuration.

        Args:
            source_lang: ISO source language code (e.g. 'en').
            task_type: One of 'translation', 'tts', 'asr'.
            target_lang: ISO target language code (required for translation).

        Returns:
            Pipeline configuration dict from Bhashini API.

        Raises:
            RuntimeError: If the pipeline fetch fails.
        """
        cache_key = f"{task_type}:{source_lang}:{target_lang}"
        if cache_key in self._pipeline_cache:
            return self._pipeline_cache[cache_key]

        task_map = {
            "translation": "translation",
            "tts": "tts",
            "asr": "asr",
        }
        bhashini_task = task_map.get(task_type, task_type)

        pipeline_tasks: list[dict] = [
            {
                "taskType": bhashini_task,
                "config": {
                    "language": {
                        "sourceLanguage": source_lang,
                        **({"targetLanguage": target_lang} if target_lang else {}),
                    }
                },
            }
        ]

        try:
            resp = await self._http.post(
                self._pipeline_url,
                json={"pipelineTasks": pipeline_tasks, "pipelineRequestConfig": {"pipelineId": "64392f96daac500b55c543cd"}},
                timeout=15.0,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logfire.error(
                "BhashiniClient._get_pipeline HTTP error",
                status=exc.response.status_code,
                task=task_type,
            )
            raise RuntimeError(
                f"Bhashini pipeline fetch failed: HTTP {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            logfire.error("BhashiniClient._get_pipeline error", error=str(exc))
            raise RuntimeError(f"Bhashini pipeline fetch error: {exc}") from exc

        pipeline_config = resp.json()
        self._pipeline_cache[cache_key] = pipeline_config
        return pipeline_config

    async def translate(
        self, text: str, source_lang: str, target_lang: str
    ) -> str:
        """
        Translate text between two languages using Bhashini.

        Args:
            text: Source text to translate.
            source_lang: ISO source language code (e.g. 'en').
            target_lang: ISO target language code (e.g. 'te').

        Returns:
            Translated text string.

        Raises:
            RuntimeError: On pipeline fetch or inference failure.
        """
        logfire.debug(
            "BhashiniClient.translate",
            src=source_lang,
            tgt=target_lang,
            chars=len(text),
        )
        t_start = time.time()
        pipeline = await self._get_pipeline(source_lang, "translation", target_lang)
        inference_url = (
            pipeline.get("pipelineInferenceAPIEndPoint", {})
            .get("callbackUrl", "")
        )
        api_key_value = (
            pipeline.get("pipelineInferenceAPIEndPoint", {})
            .get("inferenceApiKey", {})
            .get("value", "")
        )

        try:
            resp = await self._http.post(
                inference_url,
                json={
                    "pipelineTasks": [
                        {
                            "taskType": "translation",
                            "config": {
                                "language": {
                                    "sourceLanguage": source_lang,
                                    "targetLanguage": target_lang,
                                },
                                "serviceId": pipeline.get("pipelineResponseConfig", [{}])[0]
                                .get("config", [{}])[0]
                                .get("serviceId", ""),
                            },
                        }
                    ],
                    "inputData": {"input": [{"source": text}]},
                },
                headers={"Authorization": api_key_value},
                timeout=20.0,
            )
            resp.raise_for_status()
        except Exception as exc:
            logfire.error("BhashiniClient.translate inference failed", error=str(exc))
            raise RuntimeError(f"Bhashini translate failed: {exc}") from exc

        data = resp.json()
        translated = (
            data.get("pipelineResponse", [{}])[0]
            .get("output", [{}])[0]
            .get("target", text)
        )
        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "BhashiniClient.translate success",
            src=source_lang,
            tgt=target_lang,
            duration_ms=duration_ms,
        )
        return translated

    async def tts(
        self, text: str, language: str, gender: str = "female"
    ) -> bytes:
        """
        Synthesise Indian language text to speech using Bhashini.

        Args:
            text: Text to synthesise.
            language: ISO language code (e.g. 'te', 'hi').
            gender: Voice gender preference ('male' or 'female').

        Returns:
            Raw WAV audio bytes decoded from Bhashini's base64 response.

        Raises:
            RuntimeError: On pipeline fetch, inference, or decode failure.
        """
        logfire.debug("BhashiniClient.tts", language=language, chars=len(text))
        t_start = time.time()
        pipeline = await self._get_pipeline(language, "tts")
        inference_url = (
            pipeline.get("pipelineInferenceAPIEndPoint", {})
            .get("callbackUrl", "")
        )
        api_key_value = (
            pipeline.get("pipelineInferenceAPIEndPoint", {})
            .get("inferenceApiKey", {})
            .get("value", "")
        )
        service_id = (
            pipeline.get("pipelineResponseConfig", [{}])[0]
            .get("config", [{}])[0]
            .get("serviceId", "")
        )

        try:
            resp = await self._http.post(
                inference_url,
                json={
                    "pipelineTasks": [
                        {
                            "taskType": "tts",
                            "config": {
                                "language": {"sourceLanguage": language},
                                "serviceId": service_id,
                                "gender": gender,
                                "samplingRate": 22050,
                            },
                        }
                    ],
                    "inputData": {"input": [{"source": text}]},
                },
                headers={"Authorization": api_key_value},
                timeout=30.0,
            )
            resp.raise_for_status()
        except Exception as exc:
            logfire.error("BhashiniClient.tts inference failed", error=str(exc))
            raise RuntimeError(f"Bhashini TTS failed: {exc}") from exc

        data = resp.json()
        audio_b64: str = (
            data.get("pipelineResponse", [{}])[0]
            .get("audio", [{}])[0]
            .get("audioContent", "")
        )
        if not audio_b64:
            raise RuntimeError("Bhashini TTS returned empty audio content")

        audio_bytes = base64.b64decode(audio_b64)
        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "BhashiniClient.tts success",
            language=language,
            bytes=len(audio_bytes),
            duration_ms=duration_ms,
        )
        return audio_bytes

    async def stt(self, audio_bytes: bytes, language: str) -> str:
        """
        Transcribe Indian language audio using Bhashini ASR.

        Args:
            audio_bytes: Raw audio bytes to transcribe.
            language: ISO language code of the audio.

        Returns:
            Transcribed text string.

        Raises:
            RuntimeError: On pipeline or inference failure.
        """
        logfire.debug("BhashiniClient.stt", language=language, bytes=len(audio_bytes))
        t_start = time.time()
        pipeline = await self._get_pipeline(language, "asr")
        inference_url = (
            pipeline.get("pipelineInferenceAPIEndPoint", {})
            .get("callbackUrl", "")
        )
        api_key_value = (
            pipeline.get("pipelineInferenceAPIEndPoint", {})
            .get("inferenceApiKey", {})
            .get("value", "")
        )
        service_id = (
            pipeline.get("pipelineResponseConfig", [{}])[0]
            .get("config", [{}])[0]
            .get("serviceId", "")
        )

        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        try:
            resp = await self._http.post(
                inference_url,
                json={
                    "pipelineTasks": [
                        {
                            "taskType": "asr",
                            "config": {
                                "language": {"sourceLanguage": language},
                                "serviceId": service_id,
                                "audioFormat": "wav",
                                "samplingRate": 16000,
                            },
                        }
                    ],
                    "inputData": {"audio": [{"audioContent": audio_b64}]},
                },
                headers={"Authorization": api_key_value},
                timeout=30.0,
            )
            resp.raise_for_status()
        except Exception as exc:
            logfire.error("BhashiniClient.stt inference failed", error=str(exc))
            raise RuntimeError(f"Bhashini ASR failed: {exc}") from exc

        data = resp.json()
        transcript: str = (
            data.get("pipelineResponse", [{}])[0]
            .get("output", [{}])[0]
            .get("source", "")
        )
        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "BhashiniClient.stt success",
            language=language,
            words=len(transcript.split()),
            duration_ms=duration_ms,
        )
        return transcript

    async def detect_language(self, text: str) -> str:
        """
        Detect the language of text using Unicode range analysis.

        Checks for Devanagari, Telugu, Tamil, and Kannada Unicode blocks.
        Falls back to 'en' for Latin and unrecognised scripts.

        Args:
            text: Text whose language to detect.

        Returns:
            ISO 639-1 language code string.
        """
        for char in text:
            cp = ord(char)
            if 0x0900 <= cp <= 0x097F:
                return "hi"
            if 0x0C00 <= cp <= 0x0C7F:
                return "te"
            if 0x0B80 <= cp <= 0x0BFF:
                return "ta"
            if 0x0C80 <= cp <= 0x0CFF:
                return "kn"
        return "en"

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._http.aclose()
