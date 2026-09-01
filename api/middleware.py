"""
Yaazhi API Middleware — hardened for production.

Security fixes applied (audit 2026-05-10):
  SEC-4 : API key comparison uses hmac.compare_digest (timing-safe)
  SEC-5 : /docs, /redoc, /openapi.json gated to localhost or X-Internal-Request

Middleware stack (outermost → innermost):
  TimingMiddleware → RequestLoggingMiddleware → RateLimitMiddleware → APIKeyMiddleware
"""

from __future__ import annotations

import hmac
import time
import uuid
from typing import Callable

import logfire
import redis.asyncio as aioredis
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from config.settings import settings
from core.context import YaazhiContext, set_context, clear_context


# ---------------------------------------------------------------------------
# ContextExtractionMiddleware (P1.1: Multi-User Context)
# ---------------------------------------------------------------------------

class ContextExtractionMiddleware(BaseHTTPMiddleware):
    """Extract and set user context from request headers.
    
    Extracts user_id from X-User-ID header (or defaults to 'default' for dev).
    Creates YaazhiContext and sets it for the request.
    Automatically cleaned up after response.
    """

    _PUBLIC_PATHS: frozenset[str] = frozenset(["/health", "/metrics"])

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        
        try:
            # Extract user_id from header (or use default for dev/testing)
            user_id = request.headers.get("X-User-ID", settings.default_user_id)
            if not user_id:
                user_id = "default"
            
            # Extract or generate session_id
            session_id = request.headers.get("X-Session-ID", f"sess_{uuid.uuid4()}")
            
            # Create context and set for this request
            context = YaazhiContext(
                user_id=user_id,
                session_id=session_id,
                request_id=request_id
            )
            set_context(context)
            
            # Log context setup
            logfire.debug(
                "context_extracted",
                user_id=user_id,
                session_id=session_id,
                request_id=request_id,
                path=request.url.path
            )
            
            # Process request with context available
            response = await call_next(request)
            
            # Add request ID to response headers for tracing
            response.headers["X-Request-ID"] = request_id
            
            return response
            
        except Exception as exc:
            logfire.error(
                "context_extraction_error",
                error=str(exc),
                request_id=request_id
            )
            return JSONResponse(
                {"error": "Internal server error"},
                status_code=500
            )
        finally:
            # Clean up context (important to prevent leaks)
            clear_context()


# ---------------------------------------------------------------------------
# TimingMiddleware
# ---------------------------------------------------------------------------

class TimingMiddleware(BaseHTTPMiddleware):
    """Adds X-Process-Time-Ms header to every response."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        t_start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            logfire.error("TimingMiddleware unhandled exception", error=str(exc))
            response = JSONResponse({"detail": "Internal server error"}, status_code=500)
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        response.headers["X-Process-Time-Ms"] = str(duration_ms)
        return response


# ---------------------------------------------------------------------------
# RequestLoggingMiddleware
# ---------------------------------------------------------------------------

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Structured request/response logging via logfire."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        t_start = time.perf_counter()
        logfire.debug(
            "Request received",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        try:
            response = await call_next(request)
        except Exception as exc:
            logfire.error(
                "RequestLoggingMiddleware unhandled exception",
                request_id=request_id,
                error=str(exc),
            )
            response = JSONResponse({"detail": "Internal server error"}, status_code=500)

        duration_ms = int((time.perf_counter() - t_start) * 1000)
        logfire.info(
            "Request complete",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        response.headers["X-Request-ID"] = request_id
        return response


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Redis-backed rate limiter: 60 requests per minute per IP."""

    _LIMIT: int = 60
    _WINDOW: int = 60

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True
            )
        return self._redis

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            client_ip = request.client.host if request.client else "unknown"
            r = await self._get_redis()
            key = f"ratelimit:{client_ip}"
            count = await r.incr(key)
            if count == 1:
                await r.expire(key, self._WINDOW)
            if count > self._LIMIT:
                logfire.warning("Rate limit exceeded", client_ip=client_ip, count=count)
                return JSONResponse(
                    {"detail": f"Rate limit exceeded. Max {self._LIMIT} requests/min."},
                    status_code=429,
                    headers={"Retry-After": str(self._WINDOW)},
                )
        except Exception as exc:
            logfire.warning("RateLimitMiddleware error (skipping check)", error=str(exc))
        return await call_next(request)


# ---------------------------------------------------------------------------
# APIKeyMiddleware  (SEC-4 + SEC-5 hardened)
# ---------------------------------------------------------------------------

class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Validates X-API-Key for all non-public endpoints.

    SEC-4: Uses hmac.compare_digest() — immune to timing attacks.
    SEC-5: /docs, /redoc, /openapi.json are gated to:
           - Requests from 127.0.0.1 / ::1 (localhost), OR
           - X-Internal-Request: true header (for reverse-proxy local traffic)
           Any other client receives HTTP 401.

    Always-public paths (no key required): /health, /metrics
    """

    _PUBLIC_PATHS: frozenset[str] = frozenset(["/health", "/metrics"])
    _DOC_PATHS: frozenset[str] = frozenset(["/docs", "/redoc", "/openapi.json"])
    _LOCALHOST_HOSTS: frozenset[str] = frozenset(["127.0.0.1", "::1", "localhost"])

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        if not settings.yaazhi_api_key:
            logfire.warning(
                "YAAZHI_API_KEY is not set — API running in unauthenticated dev mode"
            )

    @staticmethod
    def _is_local(request: Request) -> bool:
        """Return True if the request originates from localhost."""
        client_host = (request.client.host if request.client else "") or ""
        internal_header = request.headers.get("X-Internal-Request", "").lower()
        return client_host in APIKeyMiddleware._LOCALHOST_HOSTS or internal_header == "true"

    @staticmethod
    def _check_key(provided: str, expected: str) -> bool:
        """Timing-safe key comparison (SEC-4)."""
        if not provided or not expected:
            return False
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Always-public paths — skip all checks
        if path in self._PUBLIC_PATHS:
            return await call_next(request)

        # Documentation paths — localhost only (SEC-5)
        if any(path.startswith(doc) for doc in self._DOC_PATHS):
            if self._is_local(request):
                return await call_next(request)
            logfire.warning(
                "Docs access denied from non-local IP",
                path=path,
                client=request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                {"detail": "API documentation is only accessible from localhost."},
                status_code=401,
            )

        # Dev mode: no key configured — allow everything
        if not settings.yaazhi_api_key:
            return await call_next(request)

        # Normal API paths — require valid X-API-Key
        provided_key = request.headers.get("X-API-Key", "")
        if not self._check_key(provided_key, settings.yaazhi_api_key):
            logfire.warning(
                "Unauthorized API request",
                path=path,
                key_prefix=provided_key[:8] if provided_key else "missing",
                client=request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                {"detail": "Invalid API key"},
                status_code=401,
            )

        return await call_next(request)
