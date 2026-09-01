"""
Yaazhi Coder Agent — production-hardened.

Audit fixes applied (2026-05-10):
  SEC-2 / A4 : Sandbox uses PyodideSandbox (no network) with subprocess fallback
               that applies resource.setrlimit (RAM + process caps).
  A3          : Uses tempfile.mkdtemp() — no hardcoded /tmp paths.
  A5          : Exponential backoff on the fix loop.
  A5          : Rejects code over 500 lines before execution.
"""

from __future__ import annotations

import ast
import asyncio
import os
import random
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import logfire

from config.settings import settings
from core.guardrails import GuardrailsMiddleware, validate_code_for_execution, GuardrailViolation
from core.state import CodeResult

# ---------------------------------------------------------------------------
# Sandbox strategy selection
# ---------------------------------------------------------------------------

try:
    from langchain_sandbox import PyodideSandbox  # type: ignore
    _PYODIDE_AVAILABLE = True
except ImportError:
    _PYODIDE_AVAILABLE = False
    logfire.warning("langchain-sandbox not installed — using subprocess fallback with setrlimit")

_MAX_CODE_LINES = 500
_MAX_EXEC_SECONDS = 30
_MAX_RETRIES = 4          # exponential backoff: 2^0..2^3 = 1..8 s


# ---------------------------------------------------------------------------
# Subprocess sandbox helpers (fallback when Pyodide unavailable)
# ---------------------------------------------------------------------------

def _apply_resource_limits() -> None:
    """
    Called as preexec_fn in subprocess — runs inside the child process.
    Sets RAM cap (512 MB) and process cap (50 children).
    Only available on POSIX systems.
    """
    if sys.platform == "win32":
        return
    try:
        import resource  # type: ignore
        # Virtual address space: 512 MB
        limit_bytes = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        # Max child processes
        resource.setrlimit(resource.RLIMIT_NPROC, (50, 50))
        # CPU time: 30 seconds hard limit
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    except Exception as exc:
        # If setrlimit fails (e.g. in container without CAP_SYS_RESOURCE) log and continue
        logfire.warning("setrlimit failed (non-fatal)", error=str(exc))


async def _run_in_subprocess(code: str, sandbox_dir: str) -> tuple[str, str, int]:
    """
    Execute code in an isolated subprocess with OS-level resource limits.

    Returns (stdout, stderr, returncode).
    """
    script_path = Path(sandbox_dir) / f"script_{uuid.uuid4().hex[:8]}.py"
    script_path.write_text(code, encoding="utf-8")

    preexec = None if sys.platform == "win32" else _apply_resource_limits

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=sandbox_dir,
            preexec_fn=preexec,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_MAX_EXEC_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "", f"Execution timed out after {_MAX_EXEC_SECONDS}s", -1

        return (
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
            proc.returncode or 0,
        )
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass


async def _run_in_pyodide(code: str) -> tuple[str, str, int]:
    """Execute code in PyodideSandbox — WebAssembly, no network."""
    sandbox = PyodideSandbox(allow_net=False)
    try:
        result = await asyncio.to_thread(sandbox.run, code)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        returncode = 0 if not stderr else 1
        return stdout, stderr, returncode
    except Exception as exc:
        return "", str(exc), 1


# ---------------------------------------------------------------------------
# CoderAgent
# ---------------------------------------------------------------------------

from core.agent_registry import AgentRegistry  # noqa: E402  (avoid circular at module level)


@AgentRegistry.register("coder")
class CoderAgent:
    """
    Autonomous code writer + sandbox executor.

    Write → Validate (AST) → Execute → Fix loop with exponential backoff.
    Uses PyodideSandbox when available; falls back to subprocess + setrlimit.
    """

    def __init__(self) -> None:
        self._sandbox_dir: Optional[str] = None

    def __repr__(self) -> str:
        return f"CoderAgent(sandbox={self._sandbox_dir!r})"

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        logfire.debug("CoderAgent.ping called")
        try:
            if _PYODIDE_AVAILABLE:
                _, _, rc = await _run_in_pyodide("print('ping')")
                return rc == 0
            # subprocess ping
            tmpdir = tempfile.mkdtemp(prefix="yaazhi_coder_ping_")
            try:
                stdout, _, rc = await _run_in_subprocess("print('ping')", tmpdir)
                return rc == 0 and "ping" in stdout
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception as exc:
            logfire.error("CoderAgent.ping failed", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Code validation (AST)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_syntax(code: str) -> tuple[bool, str]:
        """Return (ok, error_message). Uses stdlib ast — no execution."""
        try:
            ast.parse(code)
            return True, ""
        except SyntaxError as exc:
            return False, f"SyntaxError at line {exc.lineno}: {exc.msg}"

    # ------------------------------------------------------------------
    # Execution entry point
    # ------------------------------------------------------------------

    async def execute_code(
        self,
        code: str,
        task_description: str = "",
        session_id: str = "",
    ) -> CodeResult:
        """
        Validate and execute Python code in an isolated sandbox.

        Rejects:
          - Code over _MAX_CODE_LINES lines (500)
          - Code that fails guardrail AST safety check
          - Code with syntax errors (reported without execution)

        On failure, retries up to _MAX_RETRIES times using an LLM to
        suggest a fix, with exponential backoff between attempts.

        Returns CodeResult with stdout, stderr, exit_code, and fix history.
        """
        logfire.info(
            "CoderAgent.execute_code started",
            task=task_description[:80],
            lines=code.count("\n"),
        )
        t_start = time.time()

        # ── Guard: code length ────────────────────────────────────────────
        line_count = code.count("\n") + 1
        if line_count > _MAX_CODE_LINES:
            msg = (
                f"Code rejected: {line_count} lines exceeds the {_MAX_CODE_LINES}-line limit. "
                "Break the task into smaller subtasks."
            )
            logfire.warning("CoderAgent: code too long", lines=line_count)
            return CodeResult(
                code=code,
                stdout="",
                stderr=msg,
                exit_code=-1,
                success=False,
                error=msg,
                language="python",
            )

        # ── Guard: safety AST check ───────────────────────────────────────
        try:
            validate_code_for_execution(code)
            safe = True
            safety_reason = ""
        except GuardrailViolation as exc:
            safe = False
            safety_reason = exc.detail
        
        if not safe:
            logfire.warning("CoderAgent: code failed safety check", reason=safety_reason)
            return CodeResult(
                code=code,
                stdout="",
                stderr=safety_reason,
                exit_code=-1,
                success=False,
                error=safety_reason,
                language="python",
            )

        # ── Guard: syntax validation ──────────────────────────────────────
        syntax_ok, syntax_err = self.validate_syntax(code)
        if not syntax_ok:
            logfire.warning("CoderAgent: syntax error", error=syntax_err)
            # Fall through to fix loop starting at attempt 0 with syntax_err

        # ── Create sandbox directory ──────────────────────────────────────
        sandbox_dir = tempfile.mkdtemp(prefix="yaazhi_sandbox_")
        self._sandbox_dir = sandbox_dir
        fix_history: list[str] = []
        current_code = code
        last_error = syntax_err if not syntax_ok else ""

        try:
            for attempt in range(_MAX_RETRIES + 1):
                # On retry attempts: ask LLM to fix the code
                if attempt > 0:
                    # VECTOR-5 FIX: add jitter to prevent thundering-herd when
                    # multiple concurrent sandbox fix-loops hit the LLM at the same time.
                    backoff = 2 ** (attempt - 1) + random.uniform(0.0, 1.0)
                    logfire.info(
                        "CoderAgent: retrying after fix",
                        attempt=attempt,
                        backoff_s=round(backoff, 2),
                    )
                    await asyncio.sleep(backoff)

                    fixed_code = await self._ask_llm_to_fix(
                        current_code, last_error, task_description
                    )
                    if fixed_code:
                        fix_history.append(
                            f"[Attempt {attempt}] Error: {last_error[:200]}\n"
                            f"Fix applied:\n{fixed_code[:300]}"
                        )
                        current_code = fixed_code

                    # Re-validate safety + syntax after fix
                    try:
                        validate_code_for_execution(current_code)
                        safe2 = True
                        safety_reason2 = ""
                    except GuardrailViolation as exc:
                        safe2 = False
                        safety_reason2 = exc.detail
                        
                    if not safe2:
                        last_error = safety_reason2
                        continue
                    syntax_ok2, syntax_err2 = self.validate_syntax(current_code)
                    if not syntax_ok2:
                        last_error = syntax_err2
                        continue

                # ── Execute ──────────────────────────────────────────────
                if _PYODIDE_AVAILABLE:
                    stdout, stderr, rc = await _run_in_pyodide(current_code)
                else:
                    stdout, stderr, rc = await _run_in_subprocess(current_code, sandbox_dir)

                logfire.info(
                    "CoderAgent.execute_code attempt result",
                    attempt=attempt,
                    exit_code=rc,
                    stdout_len=len(stdout),
                    stderr_len=len(stderr),
                )

                if rc == 0 and not stderr:
                    duration_ms = int((time.time() - t_start) * 1000)
                    logfire.info(
                        "CoderAgent.execute_code success",
                        attempt=attempt,
                        duration_ms=duration_ms,
                    )
                    return CodeResult(
                        code=current_code,
                        stdout=stdout,
                        stderr="",
                        exit_code=0,
                        success=True,
                        error=None,
                        language="python",
                        fix_history=fix_history,
                    )

                last_error = stderr or f"Non-zero exit code: {rc}"

            # All retries exhausted
            logfire.error(
                "CoderAgent.execute_code failed after all retries",
                last_error=last_error[:200],
            )
            return CodeResult(
                code=current_code,
                stdout="",
                stderr=last_error,
                exit_code=-1,
                success=False,
                error=last_error,
                language="python",
                fix_history=fix_history,
            )

        finally:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
            self._sandbox_dir = None

    def execute_safe(
        self,
        code: str,
        task_description: str = "",
        session_id: str = "",
    ) -> CodeResult:
        """
        Synchronous wrapper for execute_code for backwards compatibility with
        tests that call execute_safe() without awaiting.

        Performs the safety AST check synchronously and raises GuardrailViolation
        if unsafe. Otherwise runs the async execute_code via asyncio.run().
        """
        # Synchronous safety check
        try:
            validate_code_for_execution(code)
        except GuardrailViolation:
            # Raise to caller synchronously as tests expect
            raise
        except Exception:
            # If validator not available or other issue, continue to execution
            pass

        # Execute the async method in a fresh event loop
        try:
            return asyncio.run(self.execute_code(code, task_description=task_description, session_id=session_id))
        except RuntimeError:
            # If there's already a running loop (e.g., in some test harness), use alternative
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Run coroutine in running loop by creating a new task and waiting briefly
                coro = self.execute_code(code, task_description=task_description, session_id=session_id)
                return loop.run_until_complete(coro)
            else:
                return asyncio.run(self.execute_code(code, task_description=task_description, session_id=session_id))

    async def write_code(self, prompt: str) -> str:
        """
        Generate Python code for a prompt using the LLM and return it as a string.
        """
        try:
            import litellm  # type: ignore
            model = settings.get_litellm_model("code_generation")
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.2,
            )
            code = response.choices[0].message.content or ""
            # Strip markdown fences if present
            code = code.strip()
            if code.startswith("```python"):
                code = code[9:]
            if code.startswith("```"):
                code = code[3:]
            if code.endswith("```"):
                code = code[:-3]
            return code.strip()
        except Exception as exc:
            logfire.error("CoderAgent.write_code LLM failed", error=str(exc))
            return ""

    # ------------------------------------------------------------------
    # LLM-assisted fix
    # ------------------------------------------------------------------

    async def _ask_llm_to_fix(
        self,
        code: str,
        error: str,
        task: str,
    ) -> str:
        """Ask Claude/Groq to fix broken code. Returns fixed code or empty string."""
        try:
            import litellm  # type: ignore

            model = settings.get_litellm_model("code_generation")
            prompt = (
                f"You are a Python expert. Fix the following code so it runs without errors.\n\n"
                f"Task: {task}\n\n"
                f"Error:\n{error[:500]}\n\n"
                f"Original code:\n```python\n{code[:3000]}\n```\n\n"
                f"Return ONLY the fixed Python code — no explanation, no markdown fences."
            )
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.1,
            )
            raw = response.choices[0].message.content or ""
            # Strip markdown fences if LLM added them
            raw = raw.strip()
            if raw.startswith("```python"):
                raw = raw[9:]
            if raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            return raw.strip()
        except Exception as exc:
            logfire.warning("CoderAgent._ask_llm_to_fix failed", error=str(exc))
            return ""

    # ------------------------------------------------------------------
    # High-level generate + run
    # ------------------------------------------------------------------

    async def write_and_run(
        self,
        task: str,
        context: str = "",
        session_id: str = "",
    ) -> CodeResult:
        """
        Generate code for a task using the LLM, then execute it in the sandbox.

        Args:
            task: Natural-language description of what the code should do.
            context: Optional additional context (memories, prior results).
            session_id: Session UUID for logging.

        Returns:
            CodeResult from execute_code().
        """
        logfire.info("CoderAgent.write_and_run", task=task[:80])
        try:
            import litellm  # type: ignore

            model = settings.get_litellm_model("code_generation")
            system = (
                "You are an expert Python programmer. Write clean, safe Python 3.11 code.\n"
                "Rules:\n"
                "- Use only standard library and commonly available packages.\n"
                "- Do NOT use requests, httpx, socket, or any network library.\n"
                "- Do NOT read or write files outside the current directory.\n"
                "- Do NOT use os.system, subprocess, eval, exec, or __import__.\n"
                "- Keep code under 200 lines.\n"
                "- Include a if __name__ == '__main__': block that demonstrates the solution.\n"
                "Return ONLY Python code — no explanation, no markdown fences."
            )
            user_msg = f"Task: {task}"
            if context:
                user_msg += f"\n\nContext:\n{context[:2000]}"

            response = await litellm.acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=2000,
                temperature=0.2,
            )
            code = response.choices[0].message.content or ""
            code = code.strip()
            if code.startswith("```python"):
                code = code[9:]
            if code.startswith("```"):
                code = code[3:]
            if code.endswith("```"):
                code = code[:-3]
            code = code.strip()

        except Exception as exc:
            logfire.error("CoderAgent.write_and_run LLM generation failed", error=str(exc))
            return CodeResult(
                code="",
                stdout="",
                stderr=str(exc),
                exit_code=-1,
                success=False,
                error=str(exc),
                language="python",
            )

        return await self.execute_code(code, task_description=task, session_id=session_id)

    async def close(self) -> None:
        """Clean up any leftover sandbox directory."""
        if self._sandbox_dir and Path(self._sandbox_dir).exists():
            shutil.rmtree(self._sandbox_dir, ignore_errors=True)
            self._sandbox_dir = None
