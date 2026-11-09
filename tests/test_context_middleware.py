"""Regression tests for request-scoped user and session context."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware import ContextExtractionMiddleware
from core.context import get_context


def test_context_middleware_is_registered_by_application(test_client):
    """Requests through the application expose the authenticated user context."""
    response = test_client.get(
        "/health",
        headers={"X-User-ID": "alice", "X-Session-ID": "session-1"},
    )

    assert response.status_code == 200
    assert any(
        middleware.cls is ContextExtractionMiddleware
        for middleware in test_client.app.user_middleware
    )


def test_context_middleware_cleans_up_after_request():
    """Request context must not leak into the caller's async context."""
    app = FastAPI()

    @app.get("/context")
    async def context_snapshot() -> dict[str, str]:
        context = get_context()
        return {
            "user_id": context.user_id,
            "session_id": context.session_id,
        }

    app.add_middleware(ContextExtractionMiddleware)

    with TestClient(app) as client:
        response = client.get(
            "/context",
            headers={"X-User-ID": "bob", "X-Session-ID": "session-2"},
        )

    assert response.json() == {
        "user_id": "bob",
        "session_id": "session-2",
    }
