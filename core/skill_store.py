"""
Yaazhi Skill Store — GitHub HTTP Fetching, Local Disk I/O, and Multi-VPS Redis Sync.

Extracted from `core/skill_manager.py` to keep all skill modules focused and
under 350 lines.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

import aiofiles
import httpx
import logfire

from core.skill_scanner import (
    MAX_FILES_PER_SKILL,
    GitHubRef,
    SkillManifest,
    SkillSecurityReport,
    SkillType,
    StagedSkill,
    parse_skill_md,
    sanitize_skill_name,
    scan_skill_files,
    validate_relative_skill_path,
)

REDIS_STAGED_PREFIX = "yaazhi:skill:staged"
REDIS_INSTALLED_HASH = "yaazhi:skills:installed"
REDIS_TTL = 86400


async def fetch_github_files(
    ref: GitHubRef, check_url: Callable[[str], None]
) -> dict[str, str]:
    """Download skill files (.md, .py, .yaml, .json, .txt) from GitHub API or raw URL."""
    files: dict[str, str] = {}
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Yaazhi-SkillManager/1.0",
    }
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
        if ref.is_single_file:
            raw_url = f"https://raw.githubusercontent.com/{ref.owner}/{ref.repo}/{ref.branch}/{ref.subpath}"
            check_url(raw_url)
            resp = await client.get(raw_url)
            resp.raise_for_status()
            files[validate_relative_skill_path(Path(ref.subpath).name or "SKILL.md")] = resp.text
            return files

        api_url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/contents/{ref.subpath.strip('/')}?ref={ref.branch}"
        check_url(api_url)
        resp = await client.get(api_url)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("type") == "file":
            dl_url = payload.get("download_url")
            if dl_url:
                check_url(dl_url)
                f_resp = await client.get(dl_url)
                f_resp.raise_for_status()
                files[validate_relative_skill_path(payload["name"])] = f_resp.text
            return files
        if not isinstance(payload, list):
            raise ValueError("Unexpected response format from GitHub Contents API.")

        for item in payload:
            if len(files) >= MAX_FILES_PER_SKILL or not isinstance(item, dict):
                break
            item_type, item_name = item.get("type"), str(item.get("name", ""))
            if item_type == "file" and item_name.endswith((".md", ".py", ".yaml", ".yml", ".json", ".txt")):
                dl_url = item.get("download_url")
                if dl_url:
                    check_url(dl_url)
                    f_resp = await client.get(dl_url)
                    f_resp.raise_for_status()
                    files[validate_relative_skill_path(item_name)] = f_resp.text
            elif item_type == "dir" and item_name in ("scripts", "resources", "examples", "references"):
                sub_path = f"{ref.subpath.strip('/')}/{item_name}".strip("/")
                sub_api = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/contents/{sub_path}?ref={ref.branch}"
                check_url(sub_api)
                sub_resp = await client.get(sub_api)
                if sub_resp.status_code == 200 and isinstance(sub_resp.json(), list):
                    for sub_item in sub_resp.json():
                        if len(files) >= MAX_FILES_PER_SKILL:
                            break
                        if isinstance(sub_item, dict) and sub_item.get("type") == "file":
                            sub_name, sub_dl = str(sub_item.get("name", "")), sub_item.get("download_url")
                            if sub_dl and sub_name.endswith((".py", ".md", ".json", ".yaml", ".txt")):
                                check_url(sub_dl)
                                sf_resp = await client.get(sub_dl)
                                sf_resp.raise_for_status()
                                files[validate_relative_skill_path(f"{item_name}/{sub_name}")] = sf_resp.text
    return files


async def write_skill_to_disk(skills_dir: Path, skill_name: str, files: dict[str, str]) -> Path:
    """Write validated relative skill files under `skills_dir/<skill_name>/`."""
    target_dir = (skills_dir / sanitize_skill_name(skill_name)).resolve()
    target_dir.relative_to(skills_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        safe_rel = validate_relative_skill_path(rel_path)
        dest = (target_dir / safe_rel).resolve()
        dest.relative_to(target_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(dest, mode="w", encoding="utf-8") as fh:
            await fh.write(content)
    return target_dir


def scan_local_skill_roots(skills_dir: Path, default_skills_root: Path) -> dict[str, SkillManifest]:
    """Scan local skill roots (`skills/`, `.agents/plugins/`, `~/.gemini/config/plugins/`)."""
    installed: dict[str, SkillManifest] = {}
    search_roots: list[Path] = []
    if skills_dir == default_skills_root.resolve():
        project_root = default_skills_root.resolve().parent
        for p_root in (
            project_root / ".agents" / "plugins",
            Path.home() / ".gemini" / "config" / "plugins",
        ):
            if p_root.exists():
                for pdir in sorted(p_root.iterdir()):
                    if (pdir / "skills").is_dir():
                        search_roots.append(pdir / "skills")
    if skills_dir.exists():
        search_roots.append(skills_dir)

    for root_dir in search_roots:
        for skill_folder in sorted(root_dir.iterdir()):
            if not skill_folder.is_dir() or not (skill_folder / "SKILL.md").exists():
                continue
            try:
                content = (skill_folder / "SKILL.md").read_text(encoding="utf-8")
                name, desc, tags, instructions = parse_skill_md(content, fallback_name=skill_folder.name)
                files: dict[str, str] = {}
                entry_script: Optional[str] = None
                for fp in sorted(skill_folder.rglob("*")):
                    if fp.is_file():
                        rel = fp.relative_to(skill_folder).as_posix()
                        files[rel] = fp.read_text(encoding="utf-8", errors="replace")
                        if rel.endswith(".py") and entry_script is None:
                            entry_script = rel
                installed[name] = SkillManifest(
                    name=name,
                    description=desc,
                    skill_type=SkillType.PYTHON_TOOL if entry_script else SkillType.SKILL_MD,
                    source_url="local",
                    instructions=instructions,
                    files=files,
                    tags=tags,
                    entry_script=entry_script,
                    installed_path=str(skill_folder),
                )
            except Exception as exc:
                logfire.warn("SkillManager: failed to load local skill", folder=str(skill_folder), error=str(exc))
    return installed


def serialize_staged(staged: StagedSkill) -> str:
    """Serialize a StagedSkill to JSON for Redis storage."""
    payload = {
        "skill_id": staged.skill_id,
        "session_id": staged.session_id,
        "approved": staged.approved,
        "installed": staged.installed,
        "manifest": {
            "name": staged.manifest.name,
            "description": staged.manifest.description,
            "skill_type": staged.manifest.skill_type.value,
            "source_url": staged.manifest.source_url,
            "instructions": staged.manifest.instructions,
            "files": staged.manifest.files,
            "tags": staged.manifest.tags,
            "entry_script": staged.manifest.entry_script,
            "installed_path": staged.manifest.installed_path,
            "updated_at": staged.manifest.updated_at,
        },
        "security_report": {
            "passed": staged.security_report.passed,
            "permission_level": staged.security_report.permission_level,
            "findings": staged.security_report.findings,
            "files_scanned": staged.security_report.files_scanned,
        },
    }
    return json.dumps(payload)


def deserialize_staged(raw: str) -> StagedSkill:
    """Deserialize a StagedSkill from Redis JSON."""
    data = json.loads(raw)
    m, sr = data["manifest"], data["security_report"]
    return StagedSkill(
        skill_id=data["skill_id"],
        session_id=data["session_id"],
        approved=data["approved"],
        installed=data["installed"],
        manifest=SkillManifest(
            name=m["name"],
            description=m["description"],
            skill_type=SkillType(m["skill_type"]),
            source_url=m["source_url"],
            instructions=m["instructions"],
            files=m["files"],
            tags=m["tags"],
            entry_script=m.get("entry_script"),
            installed_path=m.get("installed_path"),
            updated_at=m["updated_at"],
        ),
        security_report=SkillSecurityReport(
            passed=sr["passed"],
            permission_level=sr["permission_level"],
            findings=sr["findings"],
            files_scanned=sr["files_scanned"],
        ),
    )


async def sync_skills_from_redis_cluster(
    redis_client: Any,
    skills_dir: Path,
    installed: dict[str, SkillManifest],
) -> int:
    """Pull missing skills from Redis cluster hash, verify AST safety, and materialize."""
    if redis_client is None or not hasattr(redis_client, "hgetall"):
        return 0
    synced = 0
    try:
        raw_map = await redis_client.hgetall(REDIS_INSTALLED_HASH)
        if not isinstance(raw_map, dict):
            return 0
        for raw_name, raw_json in raw_map.items():
            name = raw_name.decode() if isinstance(raw_name, bytes) else str(raw_name)
            if name in installed:
                continue
            val_str = raw_json.decode() if isinstance(raw_json, bytes) else str(raw_json)
            m_data = json.loads(val_str)
            files: dict[str, str] = m_data.get("files", {})
            report = scan_skill_files(files)
            if not report.passed:
                continue
            await write_skill_to_disk(skills_dir, name, files)
            installed[name] = SkillManifest(
                name=name,
                description=m_data["description"],
                skill_type=SkillType(m_data["skill_type"]),
                source_url=m_data.get("source_url", "cluster"),
                instructions=m_data["instructions"],
                files=files,
                tags=m_data.get("tags", []),
                entry_script=m_data.get("entry_script"),
                installed_path=str(skills_dir / name),
                updated_at=m_data.get("updated_at", ""),
            )
            synced += 1
    except Exception as exc:
        logfire.warn("SkillManager.sync_from_cluster warning", error=str(exc))
    return synced

