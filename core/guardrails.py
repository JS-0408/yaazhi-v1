"""
Yaazhi Guardrails — PydanticAI-powered input/output safety layer.

Validates, sanitizes, and enforces safety rules on all data entering
and leaving the Yaazhi system. No user input ever reaches an agent
without passing through these guardrails.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path
from typing import Any, Callable, Coroutine, TypeVar

import logfire
from pydantic import BaseModel, Field, field_validator

from core.state import Language

# ─── Constants ─────────────────────────────────────────────────────────────────

# Patterns that indicate prompt-injection attempts
_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(all\s+)?previous\s+instructions"),
    re.compile(r"(?i)you\s+are\s+now\s+(a\s+)?(pirate|different|unrestricted|god|admin)"),
    re.compile(r"(?i)system\s+prompt"),
    re.compile(r"(?i)override\s+core\s+directives"),
    re.compile(r"(?i)disregard\s+guardrails"),
]

_TAG_PATTERN = re.compile(r"<[^>]+>")


def sanitize_input(user_input: str) -> str:
    sanitized = user_input
    for pattern in _INJECTION_PATTERNS:
        sanitized = pattern.sub("", sanitized)
    sanitized = _TAG_PATTERN.sub("", sanitized)
    sanitized = sanitized.strip()
    if not sanitized:
        raise GuardrailViolation("EMPTY_INPUT", "Input is empty after sanitization")
    return sanitized

# Telugu Unicode block: U+0C00–U+0C7F
_TELUGU_PATTERN: re.Pattern[str] = re.compile(r"[\u0C00-\u0C7F]")
# Hindi/Devanagari: U+0900–U+097F
_HINDI_PATTERN: re.Pattern[str] = re.compile(r"[\u0900-\u097F]")
# Tamil: U+0B80–U+0BFF
_TAMIL_PATTERN: re.Pattern[str] = re.compile(r"[\u0B80-\u0BFF]")
# Kannada: U+0C80–U+0CFF
_KANNADA_PATTERN: re.Pattern[str] = re.compile(r"[\u0C80-\u0CFF]")

# Dangerous Python patterns in code submissions
_DANGEROUS_CODE_PATTERNS = [
    re.compile(r"os\.system\s*\("),
    re.compile(r"exec\s*\("),
    re.compile(r"eval\s*\("),
    re.compile(r"subprocess\.(run|Popen|call|check_call|check_output)\s*\("),
    re.compile(r"__import__\s*\("),
    re.compile(r"open\s*\([^)]+['\"]w['\"]"),
    re.compile(r"pty\.spawn\s*\("),
]

# ─── Exceptions ────────────────────────────────────────────────────────────────

class GuardrailViolation(ValueError):
    """
    Raised when input or output fails a guardrail check.

    Attributes:
        violation_type: Short code describing what rule was broken.
        detail: Human-readable explanation.
    """

    def __init__(self, violation_type: str, detail: str) -> None:
        """
        Initialise a GuardrailViolation.

        Args:
            violation_type: Short identifier for the type of violation.
            detail: Detailed human-readable description.
        """
        self.violation_type = violation_type
        self.detail = detail
        super().__init__(f"[{violation_type}] {detail}")

    def __repr__(self) -> str:
        """Return string representation."""
        return f"GuardrailViolation(type={self.violation_type!r}, detail={self.detail[:60]!r})"


# ─── Guardrail Models ──────────────────────────────────────────────────────────

class ValidatedInput(BaseModel):
    """
    User input after full guardrail validation.

    Attributes:
        original_text: The raw user input.
        sanitized_text: Input after removing injection patterns.
        detected_language: ISO language code detected by pattern analysis.
        char_count: Length of sanitized input.
        contains_code_request: True if user seems to be requesting code.
        is_safe: Whether the input is considered safe after sanitization.
    """

    original_text: str = Field(..., min_length=1)
    sanitized_text: str = Field(..., min_length=1)
    detected_language: Language = Field(default=Language.ENGLISH)
    char_count: int = Field(default=0, ge=0)
    contains_code_request: bool = Field(default=False)
    is_safe: bool = Field(default=True)

    @field_validator("sanitized_text")
    @classmethod
    def must_not_be_empty_after_sanitize(cls, v: str) -> str:
        """Ensure input is non-empty after stripping injection patterns."""
        if not v.strip():
            raise ValueError("Input is empty after sanitization")
        return v.strip()

    def __repr__(self) -> str:
        """Return safe string representation."""
        return (
            f"ValidatedInput(lang={self.detected_language}, "
            f"chars={self.char_count}, injection_stripped={self.original_text != self.sanitized_text})"
        )


class ValidatedOutput(BaseModel):
    """
    System output after guardrail validation.

    Attributes:
        response: The final validated response text.
        confidence_score: 0.0–1.0 confidence in response quality.
        sources_used: List of source references.
        warnings: List of user-visible warnings.
    """

    response: str = Field(..., min_length=1)
    confidence_score: float = Field(default=0.8, ge=0.0, le=1.0)
    sources_used: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def __repr__(self) -> str:
        """Return concise string representation."""
        return (
            f"ValidatedOutput(confidence={self.confidence_score:.2f}, "
            f"warnings={len(self.warnings)}, sources={len(self.sources_used)})"
        )


class SafeCodeRequest(BaseModel):
    """
    A code execution request after safety validation.

    Attributes:
        code: The sanitized, safe Python code string.
        language: Programming language (currently only 'python' supported).
        timeout_seconds: Execution timeout limit.
        allow_network: Whether code is allowed to make network calls.
    """

    code: str = Field(..., min_length=1)
    language: str = Field(default="python")
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    allow_network: bool = Field(default=False)

    @field_validator("code")
    @classmethod
    def validate_code_safety(cls, v: str) -> str:
        """
        Check code for dangerous patterns before allowing execution.

        Args:
            v: Python code string to validate.

        Returns:
            The original code string if safe.

        Raises:
            ValueError: If any dangerous pattern is found.
        """
        for pattern in _DANGEROUS_CODE_PATTERNS:
            if pattern.search(v):
                raise ValueError(
                    f"Code contains disallowed pattern: {pattern.pattern!r}. "
                    "Shell commands, eval, exec, and arbitrary file writes are blocked."
                )
        # Try AST parse to catch syntax errors early
        try:
            ast.parse(v)
        except SyntaxError as exc:
            raise ValueError(f"Code has syntax error: {exc}") from exc
        return v

    def __repr__(self) -> str:
        """Return safe string representation."""
        lines = self.code.count("\n") + 1
        return f"SafeCodeRequest(lang={self.language}, lines={lines}, timeout={self.timeout_seconds}s)"


class MemoryQuery(BaseModel):
    """
    A validated semantic memory search query.

    Attributes:
        query: The search query string.
        top_k: Maximum number of results to return.
        source_filter: If set, restrict results to this source.
        session_id: If set, restrict to this conversation session.
    """

    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=50)
    source_filter: str | None = Field(default=None)
    session_id: str | None = Field(default=None)

    @field_validator("query")
    @classmethod
    def strip_query(cls, v: str) -> str:
        """Strip whitespace from memory query."""
        return v.strip()

    def __repr__(self) -> str:
        """Return concise string representation."""
        return f"MemoryQuery(query={self.query[:30]!r}, top_k={self.top_k})"


# ─── Guardrails Functions ─────────────────────────────────────────────────────

def _detect_language(text: str) -> Language:
    """
    Detect language from character patterns in text.

    Args:
        text: Input text to analyse.

    Returns:
        Detected Language enum value.
    """
    if _TELUGU_PATTERN.search(text):
        return Language.TELUGU
    if _HINDI_PATTERN.search(text):
        return Language.HINDI
    if _TAMIL_PATTERN.search(text):
        return Language.TAMIL
    if _KANNADA_PATTERN.search(text):
        return Language.KANNADA
    return Language.ENGLISH


def _strip_injection_patterns(text: str) -> str:
    return sanitize_input(text)


def _validate_file_path(file_path: str, allowed_prefix: str = "/tmp/yaazhi_sandbox") -> Path:
    """
    Validate a file path to prevent path traversal attacks.

    Args:
        file_path: The file path string to validate.
        allowed_prefix: The directory prefix that paths must start with.

    Returns:
        Resolved Path object if safe.

    Raises:
        GuardrailViolation: If path traversal is detected.
    """
    try:
        resolved = Path(file_path).resolve()
        allowed = Path(allowed_prefix).resolve()
        if resolved != allowed and allowed not in resolved.parents:
            raise GuardrailViolation(
                "PATH_TRAVERSAL",
                f"File path {file_path!r} resolves outside allowed directory {allowed_prefix!r}",
            )
        return resolved
    except (OSError, ValueError) as exc:
        raise GuardrailViolation("INVALID_PATH", f"Invalid file path {file_path!r}: {exc}") from exc


def validate_user_input(raw_input: str) -> ValidatedInput:
    """
    Run all input guardrails on raw user text.

    Args:
        raw_input: The raw text from the user.

    Returns:
        ValidatedInput with sanitized text and detected language.

    Raises:
        GuardrailViolation: If input exceeds length limits or is entirely injection.
    """
    if not raw_input or not raw_input.strip():
        raise GuardrailViolation("EMPTY_INPUT", "User input cannot be empty")

    if len(raw_input) > 2000:
        raise GuardrailViolation(
            "INPUT_TOO_LONG",
            f"Input is {len(raw_input)} characters; maximum is 2000",
        )

    sanitized = _strip_injection_patterns(raw_input)
    detected_lang = _detect_language(sanitized)

    code_keywords = ["write code", "python", "script", "function", "program", "algorithm"]
    contains_code = any(kw in sanitized.lower() for kw in code_keywords)
    is_safe = sanitized == raw_input

    logfire.info(
        "Input validated",
        original_length=len(raw_input),
        sanitized_length=len(sanitized),
        detected_language=detected_lang.value,
        injection_stripped=sanitized != raw_input,
    )

    return ValidatedInput(
        original_text=raw_input,
        sanitized_text=sanitized,
        detected_language=detected_lang,
        char_count=len(sanitized),
        contains_code_request=contains_code,
        is_safe=is_safe,
    )


def validate_output(response: str, confidence: float = 0.8) -> ValidatedOutput:
    """
    Run guardrails on system output before delivering to user.

    Args:
        response: The AI-generated response text.
        confidence: Confidence score for this response.

    Returns:
        ValidatedOutput with warnings list if issues detected.

    Raises:
        GuardrailViolation: If response is empty or clearly invalid.
    """
    if not response or not response.strip():
        raise GuardrailViolation("EMPTY_OUTPUT", "System produced empty response")

    warnings: list[str] = []

    if len(response) < 10:
        warnings.append("Response is unusually short — may be incomplete")

    if confidence < 0.4:
        warnings.append("Low confidence response — please verify with additional sources")

    logfire.info("Output validated", length=len(response), confidence=confidence, warnings=len(warnings))

    return ValidatedOutput(
        response=response.strip(),
        confidence_score=confidence,
        sources_used=[],
        warnings=warnings,
    )


def validate_code_for_execution(source_code: str) -> bool:
    for pattern in _DANGEROUS_CODE_PATTERNS:
        if re.search(pattern, source_code):
            raise GuardrailViolation("UNSAFE_CODE", f"CRITICAL: Banned execution pattern '{pattern}' isolated.")
    return True


# ─── Middleware ────────────────────────────────────────────────────────────────

_F = TypeVar("_F", bound=Callable[..., Coroutine[Any, Any, Any]])


class GuardrailsMiddleware:
    """
    Wraps any async function with input/output guardrail validation.

    Usage:
        @GuardrailsMiddleware.wrap
        async def my_func(user_input: str) -> str:
            ...
    """

    def __repr__(self) -> str:
        """Return string representation."""
        return "GuardrailsMiddleware()"

    @staticmethod
    async def ping() -> bool:
        """Health check — always returns True for stateless middleware.

        Returns:
            Always True since middleware has no external dependency.
        """
        return True

    @staticmethod
    def wrap(func: _F) -> _F:
        """
        Decorator that validates the first string argument and string return value.

        Assumes the first positional argument is the user-facing input string
        and the return value is a string to validate as output.

        Args:
            func: Async function to wrap.

        Returns:
            Wrapped async function with guardrails applied.
        """

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = __import__("time").perf_counter()
            # Validate first string argument if present
            if args and isinstance(args[0], str):
                try:
                    validated = validate_user_input(args[0])
                    args = (validated.sanitized_text,) + args[1:]
                except GuardrailViolation as exc:
                    logfire.error("Guardrail violation on input", violation=str(exc))
                    raise

            result = await func(*args, **kwargs)

            # Validate string output
            if isinstance(result, str):
                try:
                    validated_out = validate_output(result)
                    result = validated_out.response
                except GuardrailViolation as exc:
                    logfire.error("Guardrail violation on output", violation=str(exc))
                    raise

            duration_ms = int(((__import__("time").perf_counter() - start) * 1000))
            logfire.debug("Guardrail middleware completed", func=func.__name__, duration_ms=duration_ms)
            return result

        return wrapper  # type: ignore[return-value]
