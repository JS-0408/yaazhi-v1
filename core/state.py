"""
Yaazhi Core State Definitions.

Defines the shared LangGraph state TypedDict and all Pydantic models
used across the entire Yaazhi system. Import from this module only —
no circular imports.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, TypedDict

from pydantic import BaseModel, Field, field_validator, model_validator


# ─── Enums ────────────────────────────────────────────────────────────────────

class TaskType(str, Enum):
    """Enumeration of all Yaazhi agent task types."""

    RESEARCH = "research"
    CODE = "code"
    READ_DOC = "read_doc"
    BROWSE = "browse"
    NOTIFY = "notify"
    MEMORY = "memory"
    VOICE = "voice"
    REVIEW = "review"
    PRIVATE = "private"


class ReviewVerdict(str, Enum):
    """Possible outcomes of the Reviewer agent's evaluation."""

    PASS = "PASS"
    REVISE = "REVISE"
    FAIL = "FAIL"


class Language(str, Enum):
    """Supported natural languages."""

    ENGLISH = "en"
    TELUGU = "te"
    HINDI = "hi"
    TAMIL = "ta"
    KANNADA = "kn"
    MARATHI = "mr"
    AUTO = "auto"


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class SubTask(BaseModel):
    """
    A single decomposed unit of work within a TaskPlan.

    Attributes:
        task_id: Unique identifier for this subtask.
        task_type: Which agent will handle this task.
        description: Human-readable description of what to do.
        priority: Execution priority (1 = highest).
        dependencies: task_id values that must complete before this one.
        estimated_duration_seconds: Rough time estimate.
        requires_human_approval: True if action touches sensitive resources.
    """

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_type: TaskType
    description: str = Field(..., min_length=1, max_length=1000)
    priority: int = Field(default=5, ge=1, le=10)
    dependencies: list[str] = Field(default_factory=list)
    estimated_duration_seconds: int = Field(default=30, ge=1)
    requires_human_approval: bool = Field(default=False)

    @field_validator("description")
    @classmethod
    def strip_description(cls, v: str) -> str:
        """Strip whitespace from task description."""
        return v.strip()

    def __repr__(self) -> str:
        """Return concise string representation."""
        return f"SubTask(id={self.task_id[:8]}, type={self.task_type}, priority={self.priority})"


class TaskPlan(BaseModel):
    """
    A complete execution plan produced by the Planner agent.

    Attributes:
        plan_id: Unique plan identifier.
        tasks: Ordered list of SubTasks to execute.
        estimated_total_duration_seconds: Sum of all task estimates.
        requires_human_approval: True if any subtask does.
        created_at: UTC timestamp of plan creation.
    """

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tasks: list[SubTask] = Field(..., min_length=1, max_length=7)
    estimated_total_duration_seconds: int = Field(default=60, ge=1)
    strategy: str = Field(default="parallel")
    requires_human_approval: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("tasks")
    @classmethod
    def validate_tasks(cls, v: list[SubTask]) -> list[SubTask]:
        """Ensure task count is within allowed range."""
        if len(v) > 7:
            raise ValueError("TaskPlan cannot have more than 7 subtasks")
        return v

    def __repr__(self) -> str:
        """Return concise string representation."""
        return f"TaskPlan(id={self.plan_id[:8]}, tasks={len(self.tasks)})"


class AgentOutput(BaseModel):
    """
    Standardised output from any Yaazhi agent.

    Attributes:
        agent_name: Name of the agent that produced this output.
        task_id: SubTask this output corresponds to.
        content: The main textual result.
        success: Whether the agent completed successfully.
        error_message: Error details if success is False.
        model_used: LiteLLM model string that produced this.
        duration_ms: How long the agent took.
        metadata: Any extra data the agent wants to pass forward.
    """

    agent_name: str = Field(..., min_length=1)
    task_id: str = Field(...)
    content: str = Field(default="")
    success: bool = Field(default=True)
    error_message: Optional[str] = Field(default=None)
    model_used: str = Field(default="")
    duration_ms: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def __repr__(self) -> str:
        """Return concise string representation."""
        status = "✓" if self.success else "✗"
        return f"AgentOutput({status} {self.agent_name}, task={self.task_id[:8]}, {self.duration_ms}ms)"


class ReviewResult(BaseModel):
    """
    Structured output from the Reviewer agent.

    Attributes:
        verdict: PASS, REVISE, or FAIL.
        relevance_score: 0–10 relevance to original task.
        completeness_score: 0–10 coverage of requirements.
        accuracy_score: 0–10 factual correctness.
        safety_score: 0–10 safety and appropriateness.
        total_score: Sum of all four scores (max 40).
        feedback: Specific actionable feedback for REVISE verdict.
        retry_with_context: Additional context to inject on retry.
    """

    verdict: ReviewVerdict
    relevance_score: int = Field(..., ge=0, le=10)
    completeness_score: int = Field(..., ge=0, le=10)
    accuracy_score: int = Field(..., ge=0, le=10)
    safety_score: int = Field(..., ge=0, le=10)
    total_score: int = Field(default=0, ge=0, le=40)
    feedback: str = Field(default="")
    retry_with_context: str = Field(default="")

    def __repr__(self) -> str:
        """Return concise string representation."""
        return f"ReviewResult(verdict={self.verdict}, score={self.total_score}/40)"

    @model_validator(mode="after")
    def compute_total(self) -> "ReviewResult":
        """Auto-compute total_score from component scores if not explicitly set."""
        if self.total_score == 0:
            self.total_score = (
                self.relevance_score
                + self.completeness_score
                + self.accuracy_score
                + self.safety_score
            )
        return self


class YaazhiInput(BaseModel):
    """
    Validated input entering the Yaazhi system.

    Attributes:
        user_input: The raw user request (sanitized).
        session_id: Conversation session identifier.
        language: Detected or declared language.
        stream: Whether to stream the response.
    """

    user_input: str = Field(..., min_length=1, max_length=2000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    language: Language = Field(default=Language.AUTO)
    stream: bool = Field(default=False)

    @field_validator("user_input")
    @classmethod
    def sanitize_input(cls, v: str) -> str:
        """Strip leading/trailing whitespace from user input."""
        return v.strip()

    def __repr__(self) -> str:
        """Return safe string representation."""
        preview = self.user_input[:40] + "..." if len(self.user_input) > 40 else self.user_input
        return f"YaazhiInput(session={self.session_id[:8]}, input={preview!r})"


class YaazhiOutput(BaseModel):
    """
    Validated final output from the Yaazhi system.

    Attributes:
        response: The final answer or result text.
        session_id: Conversation session identifier.
        confidence_score: 0.0–1.0 confidence in the response.
        sources_used: List of source URLs or document names.
        agents_called: Names of agents that contributed.
        warnings: Any warnings to surface to the user.
        processing_time_ms: Total end-to-end latency.
        model_used: Primary model that produced final answer.
        detected_language: ISO language code of the input.
        memories_used: Number of memory chunks retrieved.
    """

    response: str = Field(...)
    session_id: str = Field(...)
    confidence_score: float = Field(default=0.8, ge=0.0, le=1.0)
    sources_used: list[str] = Field(default_factory=list)
    agents_called: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    processing_time_ms: int = Field(default=0, ge=0)
    model_used: str = Field(default="")
    detected_language: str = Field(default="en")
    memories_used: int = Field(default=0, ge=0)

    def __repr__(self) -> str:
        """Return concise string representation."""
        preview = self.response[:40] + "..." if len(self.response) > 40 else self.response
        return f"YaazhiOutput(session={self.session_id[:8]}, confidence={self.confidence_score:.2f}, response={preview!r})"


class MemoryResult(BaseModel):
    """
    A single result from semantic memory search.

    Attributes:
        memory_id: Unique identifier of the memory chunk.
        text: The text content of the memory.
        score: Cosine similarity score (0.0–1.0).
        source: Origin of the memory (file name, URL, or 'manual').
        metadata: Additional metadata stored with this memory.
        created_at: When this memory was stored.
    """

    memory_id: str = Field(...)
    text: str = Field(...)
    score: float = Field(..., ge=0.0, le=1.0)
    source: str = Field(default="manual")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = Field(default=None)

    def __repr__(self) -> str:
        """Return concise string representation."""
        preview = self.text[:50] + "..." if len(self.text) > 50 else self.text
        return f"MemoryResult(id={self.memory_id[:8]}, score={self.score:.3f}, text={preview!r})"


class DocumentResult(BaseModel):
    """
    Structured output from the ReaderAgent or ResearcherAgent.

    Attributes:
        summary: High-level summary of the document content.
        key_facts: Bullet-point list of key facts extracted.
        sources: Source URLs or file paths.
        confidence_score: 0.0–1.0 confidence in the extraction.
        raw_text: Full extracted text (may be truncated for large docs).
        page_count: Number of pages (for PDFs).
        word_count: Approximate word count.
    """

    summary: str = Field(...)
    key_facts: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    confidence_score: float = Field(default=0.8, ge=0.0, le=1.0)
    raw_text: str = Field(default="")
    page_count: int = Field(default=0, ge=0)
    word_count: int = Field(default=0, ge=0)

    def __repr__(self) -> str:
        """Return concise string representation."""
        return f"DocumentResult(pages={self.page_count}, facts={len(self.key_facts)}, confidence={self.confidence_score:.2f})"


class BrowserResult(BaseModel):
    """
    Result from the BrowserAgent after a web interaction.

    Attributes:
        success: Whether the browser action completed successfully.
        url: The final URL after navigation.
        extracted_data: Structured data extracted from the page.
        screenshot_path: Path to error screenshot if applicable.
        page_title: HTML title of the page.
        error: Error message if success is False.
        duration_ms: How long the browser action took.
    """

    success: bool = Field(...)
    url: str = Field(default="")
    extracted_data: dict[str, Any] = Field(default_factory=dict)
    screenshot_path: Optional[str] = Field(default=None)
    page_title: str = Field(default="")
    error: Optional[str] = Field(default=None)
    duration_ms: int = Field(default=0, ge=0)

    def __repr__(self) -> str:
        """Return concise string representation."""
        status = "✓" if self.success else "✗"
        return f"BrowserResult({status} url={self.url!r}, {self.duration_ms}ms)"


class CodeResult(BaseModel):
    """
    Result from the CoderAgent after writing and running code.

    Attributes:
        code: The final generated Python code.
        stdout: stdout from code execution.
        stderr: stderr from code execution.
        success: True if code ran without errors.
        attempts_taken: How many write/fix iterations were needed.
        explanation: Natural language explanation of what the code does.
        return_code: Process return code (0 = success).
        error: Error details if execution failed.
        output: Legacy alias for stdout.
        language: Programming language of the generated code.
        fix_history: History of attempted automated fixes.
    """

    code: str = Field(...)
    stdout: str = Field(default="")
    stderr: str = Field(default="")
    success: bool = Field(...)
    attempts_taken: int = Field(default=1, ge=1)
    explanation: str = Field(default="")
    return_code: int = Field(default=0)
    exit_code: int = Field(default=0)
    error: Optional[str] = Field(default=None)
    output: str = Field(default="")
    language: str = Field(default="python")
    fix_history: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def sync_aliases(self) -> "CodeResult":
        if not self.output and self.stdout:
            self.output = self.stdout
        if not self.stdout and self.output:
            self.stdout = self.output
        if self.exit_code == 0 and self.return_code != 0:
            self.exit_code = self.return_code
        if self.return_code == 0 and self.exit_code != 0:
            self.return_code = self.exit_code
        if self.error is None and self.stderr:
            self.error = self.stderr
        return self

    # Backwards-compatible property aliases: some callers/tests expect attributes
    # named stdout/stderr/error to map to different internal names (output, logs, error_message).
    @property
    def stdout(self) -> str:  # type: ignore[override]
        """Alias for legacy 'stdout' — prefer .output for canonical name."""
        # If model produced 'output' use that, otherwise fallback to stored stdout field
        return getattr(self, "output", "") or getattr(self, "_stdout", "") or ""

    @stdout.setter
    def stdout(self, value: str) -> None:  # type: ignore[override]
        # Keep 'output' and internal _stdout in sync
        try:
            object.__setattr__(self, "output", value)
        except Exception:
            pass
        try:
            object.__setattr__(self, "_stdout", value)
        except Exception:
            pass

    @property
    def stderr(self) -> str:  # type: ignore[override]
        """Alias for legacy 'stderr' — maps to 'logs' when present."""
        return getattr(self, "logs", None) or getattr(self, "_stderr", "") or super().__getattribute__("stderr") if hasattr(super(), "stderr") else getattr(self, "_stderr", "")

    @stderr.setter
    def stderr(self, value: str) -> None:  # type: ignore[override]
        try:
            object.__setattr__(self, "logs", value)
        except Exception:
            pass
        try:
            object.__setattr__(self, "_stderr", value)
        except Exception:
            pass

    @property
    def error(self) -> Optional[str]:  # type: ignore[override]
        """Alias for legacy 'error' — maps to 'error_message' when present."""
        return getattr(self, "error_message", None) or getattr(self, "_error", None) or self.__dict__.get("error")

    @error.setter
    def error(self, value: Optional[str]) -> None:  # type: ignore[override]
        try:
            object.__setattr__(self, "error_message", value)
        except Exception:
            pass
        try:
            object.__setattr__(self, "_error", value)
        except Exception:
            pass

    def __repr__(self) -> str:
        """Return concise string representation."""
        status = "✓" if self.success else "✗"
        return f"CodeResult({status} attempts={self.attempts_taken}, return_code={self.return_code})"


class TranscriptResult(BaseModel):
    """
    Result from the STTEngine after audio transcription.

    Attributes:
        text: The transcribed text.
        detected_language: ISO language code detected by Whisper.
        confidence: 0.0–1.0 transcription confidence.
        duration_seconds: Audio clip length in seconds.
        word_count: Number of words transcribed.
    """

    text: str = Field(...)
    detected_language: str = Field(default="en")
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    word_count: int = Field(default=0, ge=0)

    def __repr__(self) -> str:
        """Return concise string representation."""
        preview = self.text[:40] + "..." if len(self.text) > 40 else self.text
        return f"TranscriptResult(lang={self.detected_language}, words={self.word_count}, text={preview!r})"


# ─── LangGraph State TypedDict ────────────────────────────────────────────────

class YaazhiState(TypedDict, total=False):
    """
    Shared state flowing through the LangGraph orchestrator graph.

    All nodes read from and write to this TypedDict-compatible dict.
    Keys are defined below with their types.
    """

    user_input: str
    session_id: str
    detected_language: str
    conversation_history: list[dict[str, str]]
    current_tasks: list[dict[str, Any]]
    completed_tasks: list[dict[str, Any]]
    agent_outputs: dict[str, dict[str, Any]]
    memory_context: str
    loop_count: int
    max_loops: int
    final_output: dict[str, Any]
    error_log: list[str]
    metadata: dict[str, Any]


def make_initial_state(user_input: str, session_id: str, max_loops: int = 5) -> YaazhiState:
    """
    Create a fresh initial YaazhiState for a new conversation turn.

    Args:
        user_input: The raw user message.
        session_id: The conversation session identifier.
        max_loops: Maximum reviewer loop count.

    Returns:
        A populated YaazhiState dict ready for the graph.
    """
    state = YaazhiState()
    state["user_input"] = user_input
    state["session_id"] = session_id
    state["detected_language"] = "en"
    state["conversation_history"] = []
    state["current_tasks"] = []
    state["completed_tasks"] = []
    state["agent_outputs"] = {}
    state["memory_context"] = ""
    state["loop_count"] = 0
    state["max_loops"] = max_loops
    state["final_output"] = {}
    state["error_log"] = []
    state["metadata"] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model_used": "",
        "tokens_used": 0,
    }
    return state
