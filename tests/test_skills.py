"""
tests/test_skills.py — Unit and integration tests for Open-Source / GitHub Skill Manager.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.guardrails import GuardrailViolation
from core.skill_manager import (
    SkillManager,
    SkillType,
    parse_github_source,
    parse_skill_md,
    scan_python_ast,
    scan_skill_files,
    synthesize_skill_md_from_python,
    validate_relative_skill_path,
)


# ---------------------------------------------------------------------------
# 1. GitHub Source Parsing & SSRF / Traversal Guards
# ---------------------------------------------------------------------------

def test_parse_github_shorthand():
    with patch("core.skill_manager.validate_url", side_effect=lambda u: u):
        ref = parse_github_source("anthropics/skills/skills/pdf")
    assert ref.owner == "anthropics"
    assert ref.repo == "skills"
    assert ref.subpath == "skills/pdf"
    assert ref.branch == "main"
    assert ref.is_single_file is False


def test_parse_github_tree_url():
    with patch("core.skill_manager.validate_url", side_effect=lambda u: u):
        ref = parse_github_source(
            "https://github.com/anthropics/skills/tree/develop/skills/csv-analyzer"
        )
    assert ref.owner == "anthropics"
    assert ref.repo == "skills"
    assert ref.branch == "develop"
    assert ref.subpath == "skills/csv-analyzer"
    assert ref.is_single_file is False


def test_parse_github_raw_url():
    with patch("core.skill_manager.validate_url", side_effect=lambda u: u):
        ref = parse_github_source(
            "https://raw.githubusercontent.com/owner/my-repo/main/tools/parser.py"
        )
    assert ref.owner == "owner"
    assert ref.repo == "my-repo"
    assert ref.branch == "main"
    assert ref.subpath == "tools/parser.py"
    assert ref.is_single_file is True


def test_parse_github_blocks_untrusted_host():
    with patch("core.skill_manager.validate_url", side_effect=lambda u: u):
        with pytest.raises(GuardrailViolation) as exc_info:
            parse_github_source("https://evil.example.com/owner/repo/skill.py")
    assert "untrusted_skill_host" in str(exc_info.value)


def test_parse_github_blocks_private_ip_ssrf():
    with pytest.raises(GuardrailViolation):
        parse_github_source("http://127.0.0.1:8080/owner/repo")


def test_validate_relative_skill_path_blocks_traversal():
    with pytest.raises(GuardrailViolation):
        validate_relative_skill_path("../../etc/passwd")


# ---------------------------------------------------------------------------
# 2. AST & File Security Scanner
# ---------------------------------------------------------------------------

def test_scan_skill_files_markdown_only_level_1():
    report = scan_skill_files({
        "SKILL.md": "---\nname: demo\ndescription: Demo skill\n---\n# Instructions\n",
    })
    assert report.passed is True
    assert report.permission_level == 1
    assert report.findings == []


def test_scan_skill_files_safe_python_level_2():
    report = scan_skill_files({
        "SKILL.md": "---\nname: math-tool\ndescription: Math helper\n---\n",
        "scripts/calc.py": "import math\ndef area(r: float) -> float:\n    return math.pi * r * r\n",
    })
    assert report.passed is True
    assert report.permission_level == 2
    assert report.findings == []


def test_scan_python_ast_blocks_dangerous_calls():
    bad_code = (
        "import os\n"
        "import subprocess\n"
        "os.system('rm -rf /')\n"
        "eval('1+1')\n"
    )
    findings = scan_python_ast("scripts/bad.py", bad_code)
    assert len(findings) >= 3
    joined = " | ".join(findings)
    assert "subprocess" in joined
    assert "os.system" in joined
    assert "eval" in joined


# ---------------------------------------------------------------------------
# 3. Fetch, Stage, Approve, Work On, and Run Skills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_stage_and_approve_skill_md(tmp_path: Path):
    mock_vs = AsyncMock()
    mock_vs.add = AsyncMock(return_value="skill-mem-001")

    sm = SkillManager(skills_dir=tmp_path / "skills", vector_store=mock_vs)

    fake_files = {
        "SKILL.md": (
            "---\n"
            "name: bms-analyzer\n"
            "description: Analyze BMS battery telemetry CSVs\n"
            "tags: [bms, battery, csv]\n"
            "---\n"
            "# BMS Analyzer\nAlways check voltage thresholds first.\n"
        ),
        "scripts/analyze.py": (
            "def summarize(text: str) -> str:\n"
            "    return f'Processed: {text}'\n"
            "print(summarize(globals().get('TASK_INPUT', 'default')))\n"
        ),
    }

    with (
        patch("core.skill_manager.validate_url", side_effect=lambda u: u),
        patch.object(sm, "_fetch_github_files", new_callable=AsyncMock, return_value=fake_files),
    ):
        staged = await sm.fetch_and_stage_skill("owner/bms-analyzer", session_id="sess-1")

    assert staged.manifest.name == "bms-analyzer"
    assert staged.manifest.skill_type == SkillType.PYTHON_TOOL
    assert staged.security_report.passed is True
    assert staged.security_report.permission_level == 2
    assert "bms-analyzer" in staged.human_summary()

    # Approve and install
    installed = await sm.approve_skill(staged.skill_id, session_id="sess-1")
    assert installed.name == "bms-analyzer"
    assert (tmp_path / "skills" / "bms-analyzer" / "SKILL.md").exists()
    assert (tmp_path / "skills" / "bms-analyzer" / "scripts" / "analyze.py").exists()
    mock_vs.add.assert_called_once()

    # Context matching
    ctx = sm.build_skills_context("How do I analyze BMS battery voltage?")
    assert "bms-analyzer" in ctx

    # Run skill in CoderAgent sandbox
    run_res = await sm.run_skill("bms-analyzer", task_input="Cell_01=3.7V")
    assert run_res["success"] is True
    assert "Processed: Cell_01=3.7V" in run_res["output"]


@pytest.mark.asyncio
async def test_fetch_python_repo_without_skill_md_synthesizes_manifest(tmp_path: Path):
    sm = SkillManager(skills_dir=tmp_path / "skills")
    fake_files = {
        "converter.py": (
            '""" Temperature unit conversion utilities. """\n'
            "def c_to_f(c: float) -> float:\n"
            '    """Convert Celsius to Fahrenheit."""\n'
            "    return c * 9.0 / 5.0 + 32.0\n"
        )
    }
    with (
        patch("core.skill_manager.validate_url", side_effect=lambda u: u),
        patch.object(sm, "_fetch_github_files", new_callable=AsyncMock, return_value=fake_files),
    ):
        staged = await sm.fetch_and_stage_skill("open-tools/temp-converter")

    assert staged.manifest.name == "temp-converter"
    assert "Temperature unit conversion" in staged.manifest.description
    assert "SKILL.md" in staged.manifest.files
    assert "c_to_f" in staged.manifest.files["SKILL.md"]


@pytest.mark.asyncio
async def test_approve_blocks_unsafe_skill(tmp_path: Path):
    sm = SkillManager(skills_dir=tmp_path / "skills")
    unsafe_files = {
        "SKILL.md": "---\nname: unsafe-skill\ndescription: Bad skill\n---\n",
        "scripts/exploit.py": "import os\nos.system('whoami')\n",
    }
    with (
        patch("core.skill_manager.validate_url", side_effect=lambda u: u),
        patch.object(sm, "_fetch_github_files", new_callable=AsyncMock, return_value=unsafe_files),
    ):
        staged = await sm.fetch_and_stage_skill("bad/unsafe-skill")

    assert staged.security_report.passed is False
    with pytest.raises(GuardrailViolation):
        await sm.approve_skill(staged.skill_id)
    assert not (tmp_path / "skills" / "unsafe-skill").exists()


@pytest.mark.asyncio
async def test_work_on_skill_modifies_and_tests_in_sandbox(tmp_path: Path):
    mock_coder = MagicMock()
    mock_coder.write_code = AsyncMock(
        return_value="TASK_Val = globals().get('TASK_INPUT', '')\nprint(f'Upgraded: {TASK_Val.upper()}')\n"
    )
    mock_coder.execute_code = AsyncMock(
        return_value=MagicMock(success=True, stdout="Upgraded: HELLO\n", stderr="")
    )

    sm = SkillManager(skills_dir=tmp_path / "skills", coder=mock_coder)
    fake_files = {
        "SKILL.md": "---\nname: text-formatter\ndescription: Format text\n---\n# Formatter\n",
        "scripts/format.py": "print('old')\n",
    }
    with (
        patch("core.skill_manager.validate_url", side_effect=lambda u: u),
        patch.object(sm, "_fetch_github_files", new_callable=AsyncMock, return_value=fake_files),
    ):
        staged = await sm.fetch_and_stage_skill("owner/text-formatter")
        await sm.approve_skill(staged.skill_id)

    result = await sm.work_on_skill(
        "text-formatter",
        instruction="Convert TASK_INPUT to uppercase and prefix with Upgraded:",
        test_input="hello",
    )
    assert result["sandbox_success"] is True
    assert "Upgraded: HELLO" in result["sandbox_stdout"]
    saved_code = (tmp_path / "skills" / "text-formatter" / "scripts" / "format.py").read_text(encoding="utf-8")
    assert "Upgraded:" in saved_code


@pytest.mark.asyncio
async def test_work_on_skill_blocks_unsafe_generated_code(tmp_path: Path):
    mock_coder = MagicMock()
    mock_coder.write_code = AsyncMock(return_value="import subprocess\nsubprocess.run(['ls'])\n")

    sm = SkillManager(skills_dir=tmp_path / "skills", coder=mock_coder)
    fake_files = {
        "SKILL.md": "---\nname: safe-skill\ndescription: Safe\n---\n",
        "scripts/run.py": "print('safe')\n",
    }
    with (
        patch("core.skill_manager.validate_url", side_effect=lambda u: u),
        patch.object(sm, "_fetch_github_files", new_callable=AsyncMock, return_value=fake_files),
    ):
        staged = await sm.fetch_and_stage_skill("owner/safe-skill")
        await sm.approve_skill(staged.skill_id)

    with pytest.raises(GuardrailViolation):
        await sm.work_on_skill("safe-skill", instruction="Run shell command")


# ---------------------------------------------------------------------------
# 4. FastAPI /api/v1/skills Route Tests
# ---------------------------------------------------------------------------

def test_skills_api_routes_end_to_end(test_client, tmp_path: Path):
    sm = SkillManager(skills_dir=tmp_path / "api_skills")
    test_client.app.state.skill_manager = sm

    fake_files = {
        "SKILL.md": "---\nname: git-helper\ndescription: Git workflow helper\n---\n# Git Helper\n",
        "scripts/helper.py": "print('Git helper ready')\n",
    }
    with (
        patch("core.skill_manager.validate_url", side_effect=lambda u: u),
        patch.object(sm, "_fetch_github_files", new_callable=AsyncMock, return_value=fake_files),
    ):
        resp_fetch = test_client.post(
            "/api/v1/skills/fetch",
            json={"source": "octocat/git-helper"},
            headers={"X-API-Key": "test-api-key"},
        )
    assert resp_fetch.status_code == 200
    data_fetch = resp_fetch.json()
    skill_id = data_fetch["skill_id"]
    assert data_fetch["security_passed"] is True

    resp_approve = test_client.post(
        f"/api/v1/skills/approve/{skill_id}",
        headers={"X-API-Key": "test-api-key"},
    )
    assert resp_approve.status_code == 200
    assert resp_approve.json()["installed"] is True

    resp_list = test_client.get(
        "/api/v1/skills",
        headers={"X-API-Key": "test-api-key"},
    )
    assert resp_list.status_code == 200
    names = [s["name"] for s in resp_list.json()]
    assert "git-helper" in names


@pytest.mark.asyncio
async def test_multi_vps_cluster_skill_sync(tmp_path: Path):
    """
    Multi-VPS resilience test:
      - VPS Node 1 fetches & approves an open-source skill (publishing it to shared Redis).
      - VPS Node 2 starts with an empty local `skills/` directory connected to the same Redis.
      - Calling `run_skill()` or `sync_from_cluster()` on VPS Node 2 automatically pulls
        the skill from Redis, audits its AST, materializes it locally, and executes it.
    """
    import fakeredis.aioredis as fakeredis_aioredis

    shared_redis = fakeredis_aioredis.FakeRedis()

    node1_sm = SkillManager(skills_dir=tmp_path / "vps_node_1_skills")
    node1_sm._redis = shared_redis

    node2_sm = SkillManager(skills_dir=tmp_path / "vps_node_2_skills")
    node2_sm._redis = shared_redis

    fake_files = {
        "SKILL.md": (
            "---\nname: cluster-calc\ndescription: Shared cluster skill\n---\n"
            "# Cluster Calc\n"
        ),
        "scripts/calc.py": (
            "val = int(globals().get('TASK_INPUT', '0'))\n"
            "print(f'ClusterSquare={val * val}')\n"
        ),
    }

    with (
        patch("core.skill_manager.validate_url", return_value=(True, "")),
        patch("core.skill_scanner.validate_url", return_value=(True, "")),
        patch.object(node1_sm, "_fetch_github_files", new_callable=AsyncMock, return_value=fake_files),
    ):
        staged = await node1_sm.fetch_and_stage_skill("owner/cluster-calc")
        await node1_sm.approve_skill(staged.skill_id)

    # Node 2 initially does not have cluster-calc on its local filesystem
    assert not (tmp_path / "vps_node_2_skills" / "cluster-calc").exists()

    # Running the skill on Node 2 triggers automatic Redis cluster sync + execution
    res = await node2_sm.run_skill("cluster-calc", task_input="7")
    assert res["success"] is True
    assert "ClusterSquare=49" in res["output"]
    assert (tmp_path / "vps_node_2_skills" / "cluster-calc" / "SKILL.md").exists()

