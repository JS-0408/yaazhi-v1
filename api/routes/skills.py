"""
Yaazhi Open-Source & GitHub Skills API Routes.

Routes:
  GET    /api/v1/skills                      — List installed open-source skills
  POST   /api/v1/skills/fetch                — Fetch & stage a skill from GitHub (SSRF + AST audited)
  POST   /api/v1/skills/approve/{skill_id}   — Approve & install a staged skill
  DELETE /api/v1/skills/staged/{skill_id}    — Discard a staged skill
  POST   /api/v1/skills/{skill_name}/work    — Modify / test an installed skill in CoderAgent sandbox
  POST   /api/v1/skills/{skill_name}/run     — Run an installed skill on a task input
"""

from __future__ import annotations

from typing import Any, Optional

import logfire
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.guardrails import GuardrailViolation
from core.skill_manager import SkillManager

router = APIRouter(tags=["skills"])


def _get_skill_manager(request: Request) -> SkillManager:
    state = request.app.state
    if not hasattr(state, "skill_manager") or state.skill_manager is None:
        vs = getattr(state, "vector_store", None)
        state.skill_manager = SkillManager(vector_store=vs)
    return state.skill_manager


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class FetchSkillRequest(BaseModel):
    """Payload to fetch and stage an open-source skill from GitHub."""

    source: str = Field(
        ...,
        min_length=3,
        max_length=512,
        description="GitHub repo/tree/blob URL or 'owner/repo[/subpath]' shorthand.",
    )
    session_id: str = Field(default="global")
    skill_name: Optional[str] = Field(default=None, max_length=63)


class WorkOnSkillRequest(BaseModel):
    """Payload to modify/extend an installed skill and test it in the sandbox."""

    instruction: str = Field(..., min_length=3, max_length=2000)
    test_input: str = Field(default="", max_length=1000)


class RunSkillRequest(BaseModel):
    """Payload to execute an installed skill."""

    task_input: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", summary="List all installed skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    """Return metadata for all installed skills in `skills/`."""
    sm = _get_skill_manager(request)
    installed = sm.list_installed()
    return [
        {
            "name": m.name,
            "description": m.description,
            "skill_type": m.skill_type.value,
            "source_url": m.source_url,
            "tags": m.tags,
            "entry_script": m.entry_script,
            "files": sorted(m.files.keys()),
            "updated_at": m.updated_at,
        }
        for m in installed
    ]


@router.post("/fetch", summary="Fetch and security-scan a skill from GitHub")
async def fetch_skill(request: Request, body: FetchSkillRequest) -> dict[str, Any]:
    """
    Fetch a skill folder (`SKILL.md`) or Python tool from GitHub into memory,
    run SSRF + AST static security scanning, and stage it for human approval.
    """
    sm = _get_skill_manager(request)
    try:
        staged = await sm.fetch_and_stage_skill(
            source=body.source,
            session_id=body.session_id,
            skill_name_override=body.skill_name,
        )
    except GuardrailViolation as exc:
        logfire.warning("Skill fetch blocked by guardrail", error=str(exc))
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logfire.error("Skill fetch failed", source=body.source, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Failed to fetch skill: {exc}") from exc

    return {
        "skill_id": staged.skill_id,
        "name": staged.manifest.name,
        "description": staged.manifest.description,
        "skill_type": staged.manifest.skill_type.value,
        "files": sorted(staged.manifest.files.keys()),
        "security_passed": staged.security_report.passed,
        "permission_level": staged.security_report.permission_level,
        "findings": staged.security_report.findings,
        "summary": staged.human_summary(),
    }


@router.post("/approve/{skill_id}", summary="Approve and install a staged skill")
async def approve_skill(
    request: Request,
    skill_id: str,
    session_id: str = "global",
) -> dict[str, Any]:
    """Write a staged skill to `skills/<name>/` and index it in VectorStore."""
    sm = _get_skill_manager(request)
    try:
        manifest = await sm.approve_skill(skill_id=skill_id, session_id=session_id)
    except GuardrailViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "installed": True,
        "name": manifest.name,
        "skill_type": manifest.skill_type.value,
        "installed_path": manifest.installed_path,
        "files": sorted(manifest.files.keys()),
    }


@router.delete("/staged/{skill_id}", summary="Discard a staged skill")
async def discard_staged_skill(
    request: Request,
    skill_id: str,
    session_id: str = "global",
) -> dict[str, bool]:
    """Remove a staged skill without writing any files to disk."""
    sm = _get_skill_manager(request)
    discarded = await sm.discard_staged(skill_id=skill_id, session_id=session_id)
    return {"discarded": discarded}


@router.post("/{skill_name}/work", summary="Modify and sandbox-test an installed skill")
async def work_on_skill(
    request: Request,
    skill_name: str,
    body: WorkOnSkillRequest,
) -> dict[str, Any]:
    """
    Use CoderAgent to customize/improve an installed skill, verify AST safety,
    test in the network-isolated sandbox, and save the updated script.
    """
    sm = _get_skill_manager(request)
    try:
        return await sm.work_on_skill(
            skill_name=skill_name,
            instruction=body.instruction,
            test_input=body.test_input,
        )
    except GuardrailViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{skill_name}/run", summary="Execute an installed skill")
async def run_skill(
    request: Request,
    skill_name: str,
    body: RunSkillRequest,
) -> dict[str, Any]:
    """Execute an installed skill on a task input inside CoderAgent's sandbox."""
    sm = _get_skill_manager(request)
    try:
        return await sm.run_skill(skill_name=skill_name, task_input=body.task_input)
    except GuardrailViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

