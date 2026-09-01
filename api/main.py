"""
Yaazhi FastAPI Application — Entry Point.

Startup: initialises all system components, verifies health, stores on app.state.
Shutdown: gracefully closes browser and HTTP clients.

Middleware stack (applied in order):
  1. TimingMiddleware       — X-Process-Time-Ms header
  2. RequestLoggingMiddleware — structured logfire request logs
  3. RateLimitMiddleware    — 60 req/min per IP via Redis
  4. APIKeyMiddleware       — X-API-Key header validation

Routes:
  /api/v1       → chat router
  /api/v1/memory → memory router
  /api/v1/voice  → voice router
  /health        → system health
  /api/v1/digest → daily conversation digest
  /metrics       → Prometheus metrics
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import logfire
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from agents.browser import BrowserAgent
from agents.notifier import NotifierAgent
from api.middleware import (
    APIKeyMiddleware,
    RateLimitMiddleware,
    RequestLoggingMiddleware,
    TimingMiddleware,
)
from api.routes.chat import router as chat_router
from api.routes.memory import router as memory_router
from api.routes.voice import router as voice_router
from config.settings import settings
from core.orchestrator import Yaazhi
from memory.episodic import EpisodicMemory

# A-07 FIX: PreferenceStore was a ghost import — class never existed in episodic.py.
# Implementing it here as a thin wrapper around EpisodicMemory preference methods.
class PreferenceStore:
    """Thin wrapper exposing preference get/set via EpisodicMemory."""
    def __init__(self, episodic: EpisodicMemory) -> None:
        self._ep = episodic
    async def ping(self) -> bool:
        return await self._ep.ping()
    async def set(self, key: str, value: str, user_id: str | None = None) -> None:
        await self._ep.set_preference(key, value, user_id=user_id)
    async def get(self, key: str, default: str = "", user_id: str | None = None) -> str:
        return await self._ep.get_preference(key, default=default, user_id=user_id)
from memory.ingestion import DocumentIngester
from memory.retriever import SemanticRetriever
from memory.vector_store import VectorStore
from voice.stt import STTEngine
from voice.tts import TTSEngine

_START_TIME: float = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan context manager.

    On startup: Initialise and health-check all components, store on app.state.
    On shutdown: Close browser agent and HTTP clients.

    Args:
        app: The FastAPI application instance.

    Yields:
        Control to the application.
    """
    logfire.configure(token=settings.logfire_token or None)
    logfire.info("Yaazhi API starting up", version=settings.app_version)

    # ── Initialise components ──────────────────────────────────────────────────
    vs = VectorStore()
    episodic = EpisodicMemory()
    prefs = PreferenceStore(episodic)  # A-07 FIX: pass episodic instance
    retriever = SemanticRetriever(vs)
    ingester = DocumentIngester(vs)
    browser = BrowserAgent()
    notifier = NotifierAgent()
    stt = STTEngine()
    tts = TTSEngine()
    yaazhi = Yaazhi()

    # ── Ping all components ────────────────────────────────────────────────────
    ping_results: dict[str, bool] = {}
    components = {
        "vector_store": vs,
        "episodic": episodic,
        "preferences": prefs,
        "retriever": retriever,
        "browser": browser,
        "notifier": notifier,
        "stt": stt,
        "tts": tts,
        "yaazhi": yaazhi,
    }
    for name, component in components.items():
        try:
            ping_results[name] = await component.ping()
        except Exception as exc:
            logfire.error(f"Ping failed for {name}", error=str(exc))
            ping_results[name] = False
        status = "✓" if ping_results[name] else "✗"
        logfire.info(f"Component {status} {name}", ping=ping_results[name])

    # ── Store on app.state ─────────────────────────────────────────────────────
    app.state.vector_store = vs
    app.state.episodic = episodic
    app.state.preferences = prefs
    app.state.retriever = retriever
    app.state.ingester = ingester
    app.state.browser = browser
    app.state.notifier = notifier
    app.state.stt = stt
    app.state.tts = tts
    app.state.yaazhi = yaazhi
    app.state.ping_results = ping_results

    logfire.info("Yaazhi API startup complete", healthy=sum(ping_results.values()))

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logfire.info("Yaazhi API shutting down")
    try:
        await browser.close()
    except Exception as exc:
        logfire.warning("Browser close error during shutdown", error=str(exc))
    try:
        await vs.close()
    except Exception as exc:
        logfire.warning("VectorStore close error", error=str(exc))
    logfire.info("Yaazhi API shutdown complete")


# ── Application factory ────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Yaazhi Personal AI System — REST API",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Custom middleware (applied in registration order — last is outermost) ──────
app.add_middleware(APIKeyMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(TimingMiddleware)

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(chat_router, prefix="/api/v1")
app.include_router(memory_router, prefix="/api/v1/memory")
app.include_router(voice_router, prefix="/api/v1/voice")

# ── Prometheus metrics mount ───────────────────────────────────────────────────
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


# ── Core endpoints ─────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"], summary="System health check")
async def health() -> dict:
    """
    Return the health status of all system components.

    Returns:
        Dict with status, per-service ping results, uptime_seconds, and version.
    """
    uptime = round(time.time() - _START_TIME, 2)
    ping_results: dict[str, bool] = getattr(app.state, "ping_results", {})
    all_healthy = all(ping_results.values()) if ping_results else False
    return {
        "status": "ok" if all_healthy else "degraded",
        "services": ping_results,
        "uptime_seconds": uptime,
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/digest", tags=["ops"], summary="Daily conversation digest")
async def daily_digest() -> dict:
    """
    Generate a summary of today's 'daily' conversation session.

    Returns:
        Dict with {"summary": str, "date": str}.
    """
    episodic: EpisodicMemory = app.state.episodic
    summary = await episodic.summarize_session("daily")
    return {
        "summary": summary,
        "date": datetime.now(timezone.utc).date().isoformat(),
    }


# ── Dev entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug,
        loop="uvloop",
        log_level="info",
    )
