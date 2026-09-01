"""Multi-user context management for Yaazhi.

This module provides thread-local context storage for request-scoped
user and session information. Context is automatically inherited by
async subtasks and cleaned up at request end.

Usage:
    from core.context import YaazhiContext, set_context, get_user_id
    
    ctx = YaazhiContext(
        user_id="alice@example.com",
        session_id="sess_123abc",
        request_id="req_456def"
    )
    set_context(ctx)
    user_id = get_user_id()  # "alice@example.com"
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional


@dataclass
class YaazhiContext:
    """Request-scoped user and session context.
    
    Attributes:
        user_id: Unique identifier for the user (email or UUID).
        session_id: Session token for this conversation thread.
        request_id: Correlation ID for logging/tracing (UUID format).
        permissions: Set of permission strings (loaded lazily in P1.2).
    """
    user_id: str
    session_id: str
    request_id: str
    permissions: Optional[set[str]] = None
    
    def __hash__(self) -> int:
        """Make context hashable for caching."""
        return hash((self.user_id, self.session_id))
    
    def __repr__(self) -> str:
        return f"YaazhiContext(user_id={self.user_id!r}, session_id={self.session_id!r})"


# Thread-local context storage
_context: ContextVar[Optional[YaazhiContext]] = ContextVar(
    'yaazhi_context',
    default=None
)


def set_context(context: YaazhiContext) -> Token[Optional[YaazhiContext]]:
    """Set the current context for this async task.
    
    Returns:
        A ContextVar Token that can be used to reset this context later.
    """
    return _context.set(context)


def reset_context(token: Token[Optional[YaazhiContext]]) -> None:
    """Reset the current context to a previous context token."""
    _context.reset(token)


def get_context() -> YaazhiContext:
    """Get the current context; raise if not set.
    
    Returns:
        The current YaazhiContext.
    
    Raises:
        RuntimeError: If context is not set (no request active).
    
    Example:
        >>> ctx = get_context()
        >>> print(ctx.user_id)
        "alice@example.com"
    """
    ctx = _context.get()
    if ctx is None:
        raise RuntimeError(
            "No YaazhiContext set. This usually means context was not "
            "extracted from request headers. Ensure middleware runs first."
        )
    return ctx


def get_user_id() -> str:
    """Convenience: extract user_id from current context.
    
    Returns:
        User identifier string.
    
    Raises:
        RuntimeError: If context is not set.
    """
    return get_context().user_id


def get_session_id() -> str:
    """Convenience: extract session_id from current context.
    
    Returns:
        Session identifier string.
    
    Raises:
        RuntimeError: If context is not set.
    """
    return get_context().session_id


def get_request_id() -> str:
    """Convenience: extract request_id from current context.
    
    Returns:
        Request correlation ID for tracing.
    
    Raises:
        RuntimeError: If context is not set.
    """
    return get_context().request_id


def clear_context() -> None:
    """Clear the current context (cleanup).
    
    Note:
        Automatically called at request end via middleware.
        Usually no need to call this manually.
    """
    _context.set(None)
