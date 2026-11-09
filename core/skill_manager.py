"""
Yaazhi Open-Source & GitHub Skill Manager — Multi-VPS Ready.

Manages fetching, security auditing, staging, installing, cluster-wide Redis
synchronization, sandboxed execution, and iterative modification of open-source
skills from GitHub.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import logfire

from agents.browser import validate_url  # re-exported for test patch targets
from agents.coder import CoderAgent
from config.settings import settings
from core.guardrails import GuardrailViolation
from core.skill_scanner import (
    ALLOWED_GITHUB_HOSTS,
    MAX_FILE_BYTES,
    MAX_FILES_PER_SKILL,
    GitHubRef,
    SkillManifest,
    SkillSecurityReport,
    SkillType,
    StagedSkill,
    parse_github_source,
    parse_skill_md,
    sanitize_skill_name,
    scan_python_ast,
    scan_skill_files,
    synthesize_skill_md_from_python,
    validate_relative_skill_path,
)
from core.skill_store import (
    REDIS_INSTALLED_HASH,
    REDIS_STAGED_PREFIX,
    REDIS_TTL,
    deserialize_staged,
    fetch_github_files,
    scan_local_skill_roots,
    serialize_staged,
    sync_skills_from_redis_cluster,
    write_skill_to_disk,
)

_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _check_url(url: str) -> None:
    """Validate URL using core.skill_manager.validate_url so patch targets work."""
    res = validate_url(url)
    if isinstance(res, tuple):
        valid, reason = res
        if not valid:
            raise GuardrailViolation("ssrf_blocked", reason)


class SkillManager:
    """
    Multi-VPS capable Open-Source Skill Manager.

    Installed skills are persisted both to local `skills/<name>/` and to the
    shared Redis hash `yaazhi:skills:installed` so stateless secondary VPS
    nodes automatically pull and materialize skills installed on any peer node.
    """

    _REDIS_STAGED_PREFIX = REDIS_STAGED_PREFIX
    _REDIS_INSTALLED_HASH = REDIS_INSTALLED_HASH
    _REDIS_TTL = REDIS_TTL

    def __init__(
        self,
        skills_dir: Optional[Path] = None,
        vector_store: Optional[Any] = None,
        coder: Optional[CoderAgent] = None,
    ) -> None:
        self.skills_dir = (skills_dir or _SKILLS_ROOT).resolve()
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._vs = vector_store
        self._coder = coder or CoderAgent()
        self._redis: Optional[Any] = None
        self._staged_fallback: dict[str, StagedSkill] = {}
        self._installed: dict[str, SkillManifest] = {}
        self.discover_local_skills()

    async def _ensure_redis(self) -> None:
        if self._redis is not None:
            return
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True
            )
        except Exception as exc:
            logfire.warn("SkillManager: Redis unavailable, using local fallback", error=str(exc))

    # ------------------------------------------------------------------
    # Local & Multi-VPS Cluster Discovery
    # ------------------------------------------------------------------

    def discover_local_skills(self) -> list[SkillManifest]:
        """Scan local skill roots (`skills/`, `.agents/plugins/`, `~/.gemini/config/plugins/`)."""
        self._installed = scan_local_skill_roots(self.skills_dir, _SKILLS_ROOT)
        return list(self._installed.values())

    async def sync_from_cluster(self) -> int:
        """Pull missing skills from Redis cluster hash, verify AST safety, and materialize."""
        await self._ensure_redis()
        return await sync_skills_from_redis_cluster(
            self._redis, self.skills_dir, self._installed
        )

    async def _publish_to_cluster(self, manifest: SkillManifest) -> None:
        """Publish an installed/updated skill to Redis so peer VPS nodes receive it."""
        await self._ensure_redis()
        if self._redis is None or not hasattr(self._redis, "hset"):
            return
        try:
            payload = {
                "name": manifest.name,
                "description": manifest.description,
                "skill_type": manifest.skill_type.value,
                "source_url": manifest.source_url,
                "instructions": manifest.instructions,
                "files": manifest.files,
                "tags": manifest.tags,
                "entry_script": manifest.entry_script,
                "updated_at": manifest.updated_at,
            }
            await self._redis.hset(self._REDIS_INSTALLED_HASH, manifest.name, json.dumps(payload))
        except Exception as exc:
            logfire.warn("SkillManager._publish_to_cluster failed", error=str(exc))

    async def _write_skill_to_disk(self, skill_name: str, files: dict[str, str]) -> Path:
        return await write_skill_to_disk(self.skills_dir, skill_name, files)

    def list_installed(self) -> list[SkillManifest]:
        return self.discover_local_skills()

    def get_skill(self, name: str) -> Optional[SkillManifest]:
        clean = sanitize_skill_name(name)
        if clean not in self._installed:
            self.discover_local_skills()
        return self._installed.get(clean)

    def build_skills_context(self, query: str) -> str:
        """Build a compact context block of matching installed skills for Planner."""
        skills = self.list_installed()
        if not skills:
            return ""
        q_words = {w for w in re.findall(r"[a-z0-9_]+", query.lower()) if len(w) >= 3}
        matched = [
            s for s in skills
            if any(w in f"{s.name} {s.description} {' '.join(s.tags)}".lower() for w in q_words)
        ]
        if not matched:
            return ""
        blocks = ["[INSTALLED SKILLS]"]
        for s in matched[:3]:
            blocks.append(f"- Skill `{s.name}` ({s.skill_type.value}): {s.description}\n  Instructions: {s.instructions[:500]}")
        return "\n".join(blocks)

    # ------------------------------------------------------------------
    # GitHub Fetching & Staging
    # ------------------------------------------------------------------

    async def _fetch_github_files(self, ref: GitHubRef) -> dict[str, str]:
        return await fetch_github_files(ref, check_url=_check_url)

    async def fetch_and_stage_skill(
        self, source: str, session_id: str = "global", skill_name_override: Optional[str] = None
    ) -> StagedSkill:
        with logfire.span("SkillManager.fetch_and_stage_skill", source=source):
            ref = parse_github_source(source)
            _check_url(f"https://github.com/{ref.owner}/{ref.repo}")
            raw_files = await self._fetch_github_files(ref)
            if not raw_files:
                raise ValueError(f"No supported skill files (.md, .py) found at {source!r}")

            default_name = sanitize_skill_name(
                skill_name_override or (Path(ref.subpath).stem if ref.subpath else ref.repo)
            )
            files = dict(raw_files)
            py_files = {k: v for k, v in files.items() if k.endswith(".py")}
            entry_script: Optional[str] = next(iter(sorted(py_files.keys())), None)

            skill_md_key = next((k for k in files if k.upper() == "SKILL.MD" or k.endswith("/SKILL.md")), None)
            if skill_md_key is not None:
                name, description, tags, instructions = parse_skill_md(files[skill_md_key], fallback_name=default_name)
                if skill_name_override:
                    name = sanitize_skill_name(skill_name_override)
                if skill_md_key != "SKILL.md":
                    files["SKILL.md"] = files.pop(skill_md_key)
            else:
                name = default_name
                description, synthesized_md = synthesize_skill_md_from_python(name, source, py_files)
                files["SKILL.md"] = synthesized_md
                _, _, tags, instructions = parse_skill_md(synthesized_md, fallback_name=name)

            skill_type = SkillType.PYTHON_TOOL if py_files else SkillType.SKILL_MD
            security_report = scan_skill_files(files)
            manifest = SkillManifest(
                name=name,
                description=description,
                skill_type=skill_type,
                source_url=source,
                instructions=instructions,
                files=files,
                tags=tags,
                entry_script=entry_script,
            )
            staged = StagedSkill(
                skill_id=str(uuid.uuid4())[:8],
                manifest=manifest,
                security_report=security_report,
                session_id=session_id,
            )
            await self._save_staged(staged)
            return staged

    async def _save_staged(self, staged: StagedSkill) -> None:
        self._staged_fallback[f"{staged.session_id}:{staged.skill_id}"] = staged
        await self._ensure_redis()
        if self._redis is not None:
            try:
                key = f"{self._REDIS_STAGED_PREFIX}:{staged.session_id}:{staged.skill_id}"
                await self._redis.set(key, serialize_staged(staged), ex=self._REDIS_TTL)
            except Exception as exc:
                logfire.warn("SkillManager._save_staged: Redis write failed", error=str(exc))

    async def get_staged(self, skill_id: str, session_id: str = "global") -> Optional[StagedSkill]:
        await self._ensure_redis()
        for sid in (session_id, "global"):
            if self._redis is not None:
                try:
                    raw = await self._redis.get(f"{self._REDIS_STAGED_PREFIX}:{sid}:{skill_id}")
                    if raw:
                        return deserialize_staged(raw)
                except Exception as exc:
                    logfire.warn("SkillManager.get_staged: Redis read failed", error=str(exc))
            fallback = self._staged_fallback.get(f"{sid}:{skill_id}")
            if fallback is not None:
                return fallback
        return None

    async def discard_staged(self, skill_id: str, session_id: str = "global") -> bool:
        removed = False
        for sid in (session_id, "global"):
            if self._staged_fallback.pop(f"{sid}:{skill_id}", None) is not None:
                removed = True
            if self._redis is not None:
                try:
                    if await self._redis.delete(f"{self._REDIS_STAGED_PREFIX}:{sid}:{skill_id}"):
                        removed = True
                except Exception:
                    pass
        return removed

    # ------------------------------------------------------------------
    # Approve, Work On & Run Skills
    # ------------------------------------------------------------------

    async def approve_skill(self, skill_id: str, session_id: str = "global") -> SkillManifest:
        staged = await self.get_staged(skill_id, session_id=session_id)
        if staged is None:
            raise ValueError(f"Staged skill '{skill_id}' not found or expired.")
        if not staged.security_report.passed:
            raise GuardrailViolation(
                "unsafe_skill_blocked",
                f"Cannot install skill '{staged.manifest.name}' due to security findings: "
                f"{'; '.join(staged.security_report.findings)}",
            )

        skill_name = sanitize_skill_name(staged.manifest.name)
        target_dir = await self._write_skill_to_disk(skill_name, staged.manifest.files)
        staged.approved = True
        staged.installed = True
        staged.manifest.installed_path = str(target_dir)
        self._installed[skill_name] = staged.manifest
        await self._save_staged(staged)
        await self._publish_to_cluster(staged.manifest)

        if self._vs is not None:
            try:
                summary_text = (
                    f"[Skill: {staged.manifest.name}] {staged.manifest.description}\n"
                    f"Instructions: {staged.manifest.instructions[:1000]}"
                )
                await self._vs.add(
                    summary_text,
                    metadata={
                        "skill_name": staged.manifest.name,
                        "skill_type": staged.manifest.skill_type.value,
                        "source_url": staged.manifest.source_url,
                    },
                    source="skill",
                    user_id=settings.default_user_id,
                )
            except Exception as exc:
                logfire.warn("SkillManager: failed to index skill in VectorStore", error=str(exc))
        return staged.manifest

    async def work_on_skill(self, skill_name: str, instruction: str, test_input: str = "") -> dict[str, Any]:
        manifest = self.get_skill(skill_name)
        if manifest is None:
            await self.sync_from_cluster()
            manifest = self.get_skill(skill_name)
        if manifest is None:
            raise ValueError(f"Skill '{skill_name}' is not installed.")

        target_script = manifest.entry_script or "scripts/skill_tool.py"
        existing_code = manifest.files.get(target_script, "")
        prompt = (
            f"You are working on the open-source Yaazhi skill '{manifest.name}'.\n"
            f"Skill description: {manifest.description}\n"
            f"Skill instructions:\n{manifest.instructions[:1000]}\n\n"
            f"Existing Python script ({target_script}):\n```python\n{existing_code}\n```\n\n"
            f"User modification request: {instruction}\n"
            f"Write clean, self-contained, stdlib-only Python 3.11+ code implementing the update."
        )
        new_code = await self._coder.write_code(prompt)
        findings = scan_python_ast(target_script, new_code)
        if findings:
            raise GuardrailViolation(
                "unsafe_skill_modification",
                f"Modified code for skill '{skill_name}' failed AST security audit: {'; '.join(findings)}",
            )

        sandbox_code = f"TASK_INPUT = {json.dumps(test_input)}\n{new_code}" if test_input else new_code
        exec_result = await self._coder.execute_code(sandbox_code)

        safe_rel = validate_relative_skill_path(target_script)
        manifest.files[safe_rel] = new_code
        manifest.entry_script = safe_rel
        manifest.skill_type = SkillType.PYTHON_TOOL
        manifest.updated_at = datetime.now(timezone.utc).isoformat()
        await self._write_skill_to_disk(manifest.name, manifest.files)
        self._installed[manifest.name] = manifest
        await self._publish_to_cluster(manifest)

        return {
            "skill_name": manifest.name,
            "updated_script": safe_rel,
            "code": new_code,
            "sandbox_success": exec_result.success,
            "sandbox_stdout": exec_result.stdout,
            "sandbox_stderr": exec_result.stderr,
        }

    async def run_skill(self, skill_name: str, task_input: str) -> dict[str, Any]:
        manifest = self.get_skill(skill_name)
        if manifest is None:
            await self.sync_from_cluster()
            manifest = self.get_skill(skill_name)
        if manifest is None:
            raise ValueError(f"Skill '{skill_name}' is not installed.")

        if manifest.entry_script and manifest.entry_script in manifest.files:
            code = manifest.files[manifest.entry_script]
            findings = scan_python_ast(manifest.entry_script, code)
            if findings:
                raise GuardrailViolation(
                    "unsafe_skill_execution",
                    f"Skill '{skill_name}' failed pre-execution AST check: {'; '.join(findings)}",
                )
            wrapped_code = f"TASK_INPUT = {json.dumps(task_input)}\n" + code
            exec_res = await self._coder.execute_code(wrapped_code)
            return {
                "skill_name": manifest.name,
                "mode": "sandbox_script",
                "success": exec_res.success,
                "output": exec_res.stdout or exec_res.stderr,
                "instructions": manifest.instructions,
            }

        return {
            "skill_name": manifest.name,
            "mode": "instructions_only",
            "success": True,
            "output": manifest.instructions,
            "instructions": manifest.instructions,
        }
