"""
Yaazhi Architect Agent — Self-Evolution with Permission Gate.

When the user says "add a feature to X", this agent:
  1. Writes the new agent file to agents/<name>.py
  2. Stages the pip install command (does NOT auto-run it)
  3. Updates requirements.txt + AgentRegistry
  4. Presents a human-readable DIFF and asks for YES/NO permission
  5. Only after explicit approval does it apply the changes

Permission levels:
  LEVEL_0  : Read-only analysis (no files touched)
  LEVEL_1  : Write new files only (cannot edit core/)
  LEVEL_2  : Write + install pip packages (requires confirmation)
  LEVEL_3  : Edit existing core files (requires confirmation + reason)

Usage:
    architect = ArchitectAgent()
    result = await architect.design_feature("add cryptocurrency price fetching")
    # Returns a StagedChange object — nothing applied yet
    
    if await architect.request_permission(result):
        await architect.apply(result)
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import httpx
import json
import random
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiofiles
import logfire
import redis.asyncio as aioredis

from config.settings import settings
from core.agent_registry import AgentRegistry
from core.guardrails import GuardrailViolation


# ---------------------------------------------------------------------------
# Module-level Redis ConnectionPool singleton — shared across ALL instances.
# Eliminates per-instance FD leaks when PermissionStore is recreated.
# ---------------------------------------------------------------------------

_REDIS_POOL: Optional[aioredis.ConnectionPool] = None
_MAX_CONTENT_BYTES = 4 * 1024 * 1024   # 4 MB hard ceiling per file write


def _get_redis_pool() -> aioredis.ConnectionPool:
    """Return (creating if needed) the shared module-level connection pool."""
    global _REDIS_POOL
    if _REDIS_POOL is None:
        _REDIS_POOL = aioredis.ConnectionPool.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _REDIS_POOL


async def _litellm_with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> Any:
    """
    Wrapper around litellm.acompletion with exponential backoff + jitter.

    Retries on RateLimitError and transient 5xx errors.
    Raises the last exception if all attempts exhaust.
    """
    import litellm

    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(max_attempts):
        try:
            return await litellm.acompletion(**kwargs)
        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()
            # Only retry on rate-limits and transient server errors
            if not any(kw in err_str for kw in ("429", "rate", "503", "502", "timeout", "overloaded")):
                raise
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0.0, 1.0)
                logfire.warning(
                    "litellm rate-limit / transient error — retrying",
                    attempt=attempt + 1,
                    delay_s=round(delay, 2),
                    error=str(exc)[:120],
                )
                await asyncio.sleep(delay)
    raise last_exc


# ---------------------------------------------------------------------------
# Permission levels
# ---------------------------------------------------------------------------

LEVEL_0 = 0   # Read-only — always allowed
LEVEL_1 = 1   # New files only — auto-approved in dev, requires confirmation in prod
LEVEL_2 = 2   # New files + pip install — always requires confirmation
LEVEL_3 = 3   # Edit existing core files — always requires confirmation + reason

LEVEL_LABELS = {
    LEVEL_0: "READ-ONLY",
    LEVEL_1: "CREATE FILES",
    LEVEL_2: "CREATE + INSTALL PACKAGES",
    LEVEL_3: "MODIFY CORE SYSTEM",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FileChange:
    """A single file write or modification."""
    path: str
    content: str
    is_new: bool = True
    original_content: str = ""


@dataclass
class StagedChange:
    """A complete staged evolution proposal — nothing is applied yet."""
    change_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    feature_description: str = ""
    permission_level: int = LEVEL_1
    permission_reason: str = ""
    file_changes: list[FileChange] = field(default_factory=list)
    pip_packages: list[str] = field(default_factory=list)
    agent_name: str = ""
    proposed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    approved: bool = False
    applied: bool = False
    rollback_backup: dict[str, str] = field(default_factory=dict)

    def human_summary(self) -> str:
        """Generate the human-readable permission request."""
        lines = [
            f"╔══════════════════════════════════════════════════════╗",
            f"║          🏗️  YAAZHI ARCHITECT PERMISSION REQUEST       ║",
            f"╚══════════════════════════════════════════════════════╝",
            f"",
            f"📋 Feature: {self.feature_description}",
            f"🔐 Permission Level: {LEVEL_LABELS[self.permission_level]}",
            f"📝 Reason: {self.permission_reason}",
            f"🆔 Change ID: {self.change_id}",
            f"",
        ]

        if self.file_changes:
            lines.append(f"📁 Files to be written ({len(self.file_changes)}):")
            for fc in self.file_changes:
                action = "CREATE" if fc.is_new else "MODIFY"
                lines.append(f"   [{action}] {fc.path}  ({len(fc.content)} chars)")
            lines.append("")

        if self.pip_packages:
            lines.append(f"📦 Packages to install ({len(self.pip_packages)}):")
            for pkg in self.pip_packages:
                lines.append(f"   pip install {pkg}")
            lines.append("")

        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "⚡ Reply YES to apply all changes, NO to discard.",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Permission store (Redis-backed, 24-hour TTL)
# ---------------------------------------------------------------------------

class PermissionStore:
    """Persists staged changes in Redis so they survive across chat turns."""

    _TTL = 86400   # 24 hours

    def __init__(self) -> None:
        # Use the module-level pool — no per-instance connections created.
        pass

    async def _r(self) -> aioredis.Redis:
        """Return a Redis client backed by the shared module-level pool."""
        return aioredis.Redis(connection_pool=_get_redis_pool())

    async def stage(self, change: StagedChange, session_id: str = "global") -> None:
        """C-03 FIX: keys are scoped by session_id to prevent cross-session leakage."""
        r = await self._r()
        key = f"architect:staged:{session_id}:{change.change_id}"
        await r.set(key, json.dumps(self._serialise(change)), ex=self._TTL)
        logfire.info("PermissionStore: staged change", change_id=change.change_id, session_id=session_id[:8])

    async def get(self, change_id: str, session_id: str = "global") -> Optional[StagedChange]:
        r = await self._r()
        raw = await r.get(f"architect:staged:{session_id}:{change_id}")
        if not raw:
            return None
        return self._deserialise(json.loads(raw))

    async def mark_approved(self, change_id: str, session_id: str = "global") -> None:
        r = await self._r()
        key = f"architect:staged:{session_id}:{change_id}"
        raw = await r.get(key)
        if raw:
            data = json.loads(raw)
            data["approved"] = True
            await r.set(key, json.dumps(data), ex=self._TTL)

    async def mark_applied(self, change_id: str, session_id: str = "global") -> None:
        r = await self._r()
        key = f"architect:staged:{session_id}:{change_id}"
        raw = await r.get(key)
        if raw:
            data = json.loads(raw)
            data["applied"] = True
            await r.set(key, json.dumps(data), ex=self._TTL)

    async def list_pending(self, session_id: str = "global") -> list[str]:
        """C-03 FIX: list only changes for this session, not all sessions."""
        r = await self._r()
        cursor, keys = 0, []
        # Match only this session's changes
        pattern = f"architect:staged:{session_id}:*"
        while True:
            cursor, batch = await r.scan(cursor, match=pattern, count=50)
            keys.extend(batch)
            if cursor == 0:
                break
        return [k.split(":")[-1] for k in keys]

    @staticmethod
    def _serialise(change: StagedChange) -> dict:
        return {
            "change_id": change.change_id,
            "feature_description": change.feature_description,
            "permission_level": change.permission_level,
            "permission_reason": change.permission_reason,
            "file_changes": [
                {
                    "path": fc.path,
                    "content": fc.content,
                    "is_new": fc.is_new,
                    "original_content": fc.original_content,
                }
                for fc in change.file_changes
            ],
            "pip_packages": change.pip_packages,
            "agent_name": change.agent_name,
            "proposed_at": change.proposed_at,
            "approved": change.approved,
            "applied": change.applied,
            "rollback_backup": change.rollback_backup,
        }

    @staticmethod
    def _deserialise(data: dict) -> StagedChange:
        return StagedChange(
            change_id=data["change_id"],
            feature_description=data["feature_description"],
            permission_level=data["permission_level"],
            permission_reason=data["permission_reason"],
            file_changes=[
                FileChange(
                    path=fc["path"],
                    content=fc["content"],
                    is_new=fc["is_new"],
                    original_content=fc.get("original_content", ""),
                )
                for fc in data["file_changes"]
            ],
            pip_packages=data["pip_packages"],
            agent_name=data["agent_name"],
            proposed_at=data["proposed_at"],
            approved=data["approved"],
            applied=data["applied"],
            rollback_backup=data.get("rollback_backup", {}),
        )


# ---------------------------------------------------------------------------
# ArchitectAgent
# ---------------------------------------------------------------------------

@AgentRegistry.register("architect")
class ArchitectAgent:
    """
    Self-evolving agent that can write new agents, install packages,
    and evolve Yaazhi's capabilities — all with explicit human permission.

    NEVER auto-applies changes. ALWAYS stages and asks.
    """

    # C-02 FIX: pin to file location, not CWD — works in Docker/pytest/any cwd
    _PROJECT_ROOT: Path = Path(__file__).parent.parent.resolve()
    _AGENTS_DIR: Path = _PROJECT_ROOT / "agents"
    _CORE_DIR: Path = _PROJECT_ROOT / "core"
    _REQUIREMENTS: Path = _PROJECT_ROOT / "requirements.txt"

    # Directories the architect is NEVER allowed to touch
    _FORBIDDEN_PATHS: frozenset[str] = frozenset([
        "config/.env",
        "infra/",
        ".git/",
    ])

    def __init__(self) -> None:
        self._store = PermissionStore()

    # ------------------------------------------------------------------
    # Primary entry: design_feature
    # ------------------------------------------------------------------

    async def design_feature(
        self,
        request: str,
        session_id: str = "",
    ) -> StagedChange:
        """
        Take a natural language feature request and produce a StagedChange.

        NOTHING is written to disk. The change is staged in Redis.

        Args:
            request: Natural language feature request.
            session_id: Current chat session.

        Returns:
            StagedChange ready to present to the user for approval.
        """
        logfire.info("ArchitectAgent.design_feature", request=request[:80])
        t_start = time.time()

        # Ask LLM to design the feature
        design = await self._llm_design(request)

        # Build staged change
        change = StagedChange(
            feature_description=request,
            permission_level=self._determine_level(design),
            permission_reason=design.get("reason", "New feature addition"),
            pip_packages=design.get("packages", []),
            agent_name=design.get("agent_name", ""),
        )

        # Build file changes
        for file_spec in design.get("files", []):
            path = file_spec.get("path", "")
            if not self._is_allowed_path(path):
                logfire.warning("ArchitectAgent: blocked forbidden path", path=path)
                continue

            target = self._PROJECT_ROOT / path
            is_new = not target.exists()
            original = ""
            if not is_new:
                # VECTOR-2 FIX: async file read — never blocks the event loop
                async with aiofiles.open(target, mode="r", encoding="utf-8") as fh:
                    original = await fh.read()

            change.file_changes.append(
                FileChange(
                    path=path,
                    content=file_spec.get("content", ""),
                    is_new=is_new,
                    original_content=original,
                )
            )

        # Validate all generated Python syntax before staging
        for fc in change.file_changes:
            if fc.path.endswith(".py"):
                ok, err = self._validate_syntax(fc.content)
                if not ok:
                    logfire.warning(
                        "ArchitectAgent: syntax error in generated file",
                        path=fc.path,
                        error=err,
                    )
                    # Auto-fix via LLM
                    fc.content = await self._fix_syntax(fc.content, err)

        # Stage in Redis
        await self._store.stage(change)

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "ArchitectAgent.design_feature complete",
            change_id=change.change_id,
            files=len(change.file_changes),
            packages=len(change.pip_packages),
            level=LEVEL_LABELS[change.permission_level],
            duration_ms=duration_ms,
        )
        return change

    # ------------------------------------------------------------------
    # Permission request (returns human-readable string)
    # ------------------------------------------------------------------

    async def request_permission(self, change: StagedChange) -> str:
        """
        Return the human-readable permission request string.

        This is injected into the chat response so the user sees exactly
        what Yaazhi wants to do before anything is applied.
        """
        return change.human_summary()

    # ------------------------------------------------------------------
    # Apply (only called after YES confirmation)
    # ------------------------------------------------------------------

    async def apply(self, change_id: str) -> str:
        """
        Apply a staged change after user has typed YES.

        Args:
            change_id: The 8-char change ID shown in the permission request.

        Returns:
            Human-readable success / failure report.
        """
        change = await self._store.get(change_id)
        if not change:
            return f"❌ Change ID `{change_id}` not found or expired (24h TTL)."
        if change.applied:
            return f"⚠️ Change `{change_id}` was already applied."
        if not change.approved:
            await self._store.mark_approved(change_id)

        logfire.info("ArchitectAgent.apply", change_id=change_id)
        results: list[str] = []

        # Step 1: Backup originals for rollback
        for fc in change.file_changes:
            if not fc.is_new and fc.original_content:
                change.rollback_backup[fc.path] = fc.original_content

        # Step 2: Write files
        for fc in change.file_changes:
            try:
                # VECTOR-1 FIX: enforce content size ceiling before write
                content_bytes = fc.content.encode("utf-8")
                if len(content_bytes) > _MAX_CONTENT_BYTES:
                    results.append(
                        f"❌ Rejected write to `{fc.path}`: content exceeds "
                        f"{_MAX_CONTENT_BYTES // (1024*1024)} MB safety limit."
                    )
                    logfire.error(
                        "ArchitectAgent: content too large — write blocked",
                        path=fc.path,
                        bytes=len(content_bytes),
                    )
                    continue

                target = self._PROJECT_ROOT / fc.path
                target.parent.mkdir(parents=True, exist_ok=True)
                # VECTOR-2 FIX: async file write — never blocks the event loop
                async with aiofiles.open(target, mode="w", encoding="utf-8") as fh:
                    await fh.write(fc.content)
                results.append(f"✅ {'Created' if fc.is_new else 'Updated'}: `{fc.path}`")
                logfire.info("ArchitectAgent: file written", path=fc.path)
            except Exception as exc:
                results.append(f"❌ Failed to write `{fc.path}`: {exc}")
                logfire.error("ArchitectAgent: file write failed", path=fc.path, error=str(exc))

        # Step 3: pip install packages
        for pkg in change.pip_packages:
            try:
                await self._validate_package(pkg)
                # VECTOR-1 FIX:
                #   --no-cache-dir   : avoids pip's wheel cache filling /tmp on 4GB VPS
                #   --prefer-binary  : skips C compilation, cuts peak RAM usage
                #   env MALLOC_ARENA_MAX=2 : caps glibc arena count, preventing
                #                           multi-arena memory fragmentation OOM
                import os as _os
                child_env = {**_os.environ, "MALLOC_ARENA_MAX": "2"}
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install",
                    "--no-cache-dir", "--prefer-binary", "--quiet", pkg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=child_env,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    results.append(f"❌ pip install `{pkg}` timed out (90s). Process killed.")
                    logfire.error("ArchitectAgent: pip install timeout", package=pkg)
                    continue

                if proc.returncode == 0:
                    results.append(f"✅ Installed package: `{pkg}`")
                    await self._append_requirement(pkg)
                else:
                    err_msg = stderr.decode(errors="replace")[:200]
                    results.append(f"❌ pip install `{pkg}` failed: {err_msg}")
            except Exception as exc:
                results.append(f"❌ pip install `{pkg}` exception: {exc}")

        # Step 4: Mark applied
        await self._store.mark_applied(change_id)
        change.applied = True

        report = (
            f"🏗️ **Architect Applied Change `{change_id}`**\n\n"
            + "\n".join(results)
            + "\n\n⚡ Yaazhi will detect the new agent automatically on the next request."
        )
        logfire.info("ArchitectAgent.apply complete", change_id=change_id, results=len(results))
        return report

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    async def rollback(self, change_id: str) -> str:
        """Undo an applied change using the stored backup."""
        change = await self._store.get(change_id)
        if not change or not change.applied:
            return f"❌ Change `{change_id}` not found or not yet applied."

        results: list[str] = []
        for path, original in change.rollback_backup.items():
            try:
                target = self._PROJECT_ROOT / path
                # VECTOR-2 FIX: async write in rollback path
                async with aiofiles.open(target, mode="w", encoding="utf-8") as fh:
                    await fh.write(original)
                results.append(f"↩️ Restored: `{path}`")
            except Exception as exc:
                results.append(f"❌ Rollback failed for `{path}`: {exc}")

        # Remove newly created files
        for fc in change.file_changes:
            if fc.is_new:
                try:
                    (self._PROJECT_ROOT / fc.path).unlink(missing_ok=True)
                    results.append(f"🗑️ Removed new file: `{fc.path}`")
                except Exception as exc:
                    results.append(f"❌ Could not delete `{fc.path}`: {exc}")

        return "↩️ **Rollback Complete**\n\n" + "\n".join(results)

    # ------------------------------------------------------------------
    # LLM design generation
    # ------------------------------------------------------------------

    async def _llm_design(self, request: str) -> dict:
        """Ask LLM to design the feature as a JSON specification."""
        import litellm

        model = settings.get_litellm_model("code_generation")  # A-04 FIX: "code" → "code_generation"
        system = """You are a senior Python architect for Yaazhi AI OS.
When given a feature request, produce a JSON specification for implementing it.

Rules:
1. New agents go in agents/<name>.py and MUST use @AgentRegistry.register("<name>")
2. Never modify core/orchestrator.py, config/.env, or infra/
3. Keep code concise, production-quality, fully implemented (no stubs)
4. Always add docstrings
5. List ALL pip packages needed

Output ONLY valid JSON with this structure:
{
  "agent_name": "<name or empty>",
  "reason": "<why this permission level>",
  "packages": ["package1", "package2"],
  "files": [
    {
      "path": "agents/example.py",
      "content": "<complete Python source code>"
    }
  ]
}"""

        try:
            # VECTOR-5 FIX: retry wrapper handles 429 / transient 5xx from free-tier providers
            resp = await _litellm_with_retry(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"Feature request: {request}"},
                ],
                max_tokens=4000,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content.strip()

            # VECTOR-3 FIX: regex-anchored JSON extraction — immune to fence variants
            # and conversational prefix/suffix added by the model.
            json_match = re.search(r"(\{.*\})", raw, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            return json.loads(raw)
        except Exception as exc:
            logfire.error("ArchitectAgent._llm_design failed", error=str(exc))
            return {"agent_name": "", "reason": "LLM unavailable", "packages": [], "files": []}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _validate_package(self, package_name: str) -> bool:
        """
        A-05 FIX: Stricter package validation.
        - Rejects names with chars outside [a-zA-Z0-9._-]
        - Verifies package exists on PyPI (returns HTTP 200)
        - Rejects version specifiers with shell-injection chars
        """
        # Strip version specifier for name check
        base_name = re.split(r"[>=<!@]", package_name)[0].strip()
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", base_name):
            raise GuardrailViolation(
                f"Invalid package name '{base_name}': only alphanumeric, '.', '_', '-' allowed"
            )
        # Check for shell-injection characters in the full specifier
        if any(c in package_name for c in (";", "&", "|", "`", "$", "(", ")")):
            raise GuardrailViolation(f"Shell-injection characters in package name: {package_name}")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"https://pypi.org/pypi/{base_name}/json")
            if response.status_code != 200:
                raise GuardrailViolation(f"Package not found on PyPI: {base_name}")
        logfire.info("ArchitectAgent: package validated", package=base_name)
        return True

    def _determine_level(self, design: dict) -> int:
        """Determine the permission level based on what the design touches."""
        has_packages = bool(design.get("packages"))
        touches_core = any(
            "core/" in f.get("path", "") for f in design.get("files", [])
        )
        if touches_core:
            return LEVEL_3
        if has_packages:
            return LEVEL_2
        return LEVEL_1

    def _is_allowed_path(self, path: str) -> bool:
        """Check path against forbidden list."""
        for forbidden in self._FORBIDDEN_PATHS:
            if path.startswith(forbidden) or path == forbidden:
                return False
        # Block absolute paths and traversal
        p = Path(path)
        if p.is_absolute():
            return False
        try:
            resolved = (self._PROJECT_ROOT / p).resolve()
            resolved.relative_to(self._PROJECT_ROOT.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _validate_syntax(code: str) -> tuple[bool, str]:
        try:
            ast.parse(code)
            return True, ""
        except SyntaxError as exc:
            return False, f"Line {exc.lineno}: {exc.msg}"

    async def _fix_syntax(self, code: str, error: str) -> str:
        """Ask LLM to fix a syntax error in generated code."""
        import litellm
        model = settings.get_litellm_model("code_generation")  # A-04 FIX: "code" → "code_generation"
        try:
            resp = await litellm.acompletion(
                model=model,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Fix this Python syntax error:\nError: {error}\n\n"
                        f"Code:\n```python\n{code}\n```\n\n"
                        "Return ONLY the fixed Python code, no explanation."
                    ),
                }],
                max_tokens=4000,
                temperature=0.1,
            )
            fixed = resp.choices[0].message.content.strip()
            if fixed.startswith("```python"):
                fixed = fixed[9:]
            if fixed.startswith("```"):
                fixed = fixed[3:]
            if fixed.endswith("```"):
                fixed = fixed[:-3]
            return fixed.strip()
        except Exception:
            return code   # return original if fix fails

    async def _append_requirement(self, package: str) -> None:
        """Append a new package to requirements.txt if not already present."""
        try:
            # VECTOR-2 FIX: aiofiles — both read and append are non-blocking
            async with aiofiles.open(self._REQUIREMENTS, mode="r", encoding="utf-8") as fh:
                existing = await fh.read()
            pkg_name = package.split("==")[0].split(">=")[0].split("<=")[0].strip()
            if pkg_name.lower() not in existing.lower():
                async with aiofiles.open(self._REQUIREMENTS, mode="a", encoding="utf-8") as fh:
                    await fh.write(f"\n# Added by ArchitectAgent\n{package}\n")
                logfire.info("ArchitectAgent: appended to requirements.txt", package=package)
        except Exception as exc:
            logfire.warning("ArchitectAgent._append_requirement failed", error=str(exc))

    # ------------------------------------------------------------------
    # Handle YES/NO response in chat
    # ------------------------------------------------------------------

    @classmethod
    async def handle_user_response(
        cls,
        message: str,
        session_id: str,
        store: PermissionStore,
    ) -> Optional[str]:
        """
        Check if a chat message is a YES/NO response to a pending change.

        Called by the orchestrator before normal planning.

        Returns:
            A response string if this was a YES/NO, else None (proceed normally).
        """
        msg_lower = message.strip().lower()
        pending = await store.list_pending(session_id)
        if not pending:
            return None

        # Look for YES <change_id> or just YES (applies most recent)
        words = msg_lower.split()
        if words and words[0] in ("yes", "apply", "approve", "confirm"):
            change_id = words[1] if len(words) > 1 else pending[-1]
            agent = cls()
            agent._store = store
            return await agent.apply(change_id, session_id=session_id)

        if words and words[0] in ("no", "cancel", "reject", "discard"):
            change_id = words[1] if len(words) > 1 else pending[-1]
            # VECTOR-3 FIX: key MUST include session_id — previously always missed.
            r_client = await store._r()
            await r_client.delete(f"architect:staged:{session_id}:{change_id}")
            return f"🗑️ Change `{change_id}` discarded. No files were modified."

        return None
