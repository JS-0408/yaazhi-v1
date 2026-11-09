"""
Yaazhi Open-Source Skill Scanner & Manifest Models.

Provides data models, GitHub URL/shorthand parsing with SSRF & host allowlist
validation, SKILL.md frontmatter parsing, Python AST docstring synthesis, and
static AST security scanning for open-source skills.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import logfire
import yaml

from agents.browser import validate_url
from core.guardrails import GuardrailViolation

# ---------------------------------------------------------------------------
# Constants & Security Limits
# ---------------------------------------------------------------------------

ALLOWED_GITHUB_HOSTS = frozenset({
    "github.com",
    "www.github.com",
    "api.github.com",
    "raw.githubusercontent.com",
})

MAX_FILES_PER_SKILL = 20
MAX_FILE_BYTES = 512 * 1024  # 512 KB per file

_BLOCKED_AST_CALLS = frozenset({"eval", "exec", "compile", "__import__"})
_BLOCKED_AST_MODULES = frozenset({"subprocess", "pty", "ctypes", "marshal"})
_BLOCKED_AST_ATTRS = frozenset({
    ("os", "system"), ("os", "popen"),
    ("os", "spawnl"), ("os", "spawnle"), ("os", "spawnlp"), ("os", "spawnlpe"),
    ("os", "spawnv"), ("os", "spawnve"), ("os", "spawnvp"), ("os", "spawnvpe"),
    ("os", "execl"), ("os", "execle"), ("os", "execlp"), ("os", "execlpe"),
    ("os", "execv"), ("os", "execve"), ("os", "execvp"), ("os", "execvpe"),
    ("pickle", "loads"), ("pickle", "load"),
})
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class SkillType(str, Enum):
    """Supported open-source skill formats."""

    SKILL_MD = "skill_md"
    PYTHON_TOOL = "python_tool"


@dataclass
class GitHubRef:
    """Parsed reference to a GitHub repository, branch, and subpath."""

    owner: str
    repo: str
    branch: str = "main"
    subpath: str = ""
    is_single_file: bool = False


@dataclass
class SkillSecurityReport:
    """Static analysis and safety report for a fetched skill."""

    passed: bool
    permission_level: int  # 1 = markdown only, 2 = executable python scripts
    findings: list[str] = field(default_factory=list)
    files_scanned: int = 0


@dataclass
class SkillManifest:
    """Metadata and content of an installed or staged skill."""

    name: str
    description: str
    skill_type: SkillType
    source_url: str
    instructions: str
    files: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    entry_script: Optional[str] = None
    installed_path: Optional[str] = None
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class StagedSkill:
    """A fetched skill awaiting human approval before installation."""

    skill_id: str
    manifest: SkillManifest
    security_report: SkillSecurityReport
    session_id: str = "global"
    approved: bool = False
    installed: bool = False

    def human_summary(self) -> str:
        """Render a clear human-readable approval prompt with security findings."""
        status_label = "PASSED" if self.security_report.passed else "BLOCKED (Security Violation)"
        level_str = (
            "LEVEL_1 (Markdown Instructions Only)"
            if self.security_report.permission_level == 1
            else "LEVEL_2 (Contains Executable Python Scripts)"
        )
        lines = [
            f"=== YAAZHI OPEN-SOURCE SKILL STAGED ({self.skill_id}) ===",
            f"Skill Name      : {self.manifest.name}",
            f"Type            : {self.manifest.skill_type.value}",
            f"Source          : {self.manifest.source_url}",
            f"Description     : {self.manifest.description}",
            f"Permission Gate : {level_str}",
            f"Security Audit  : {status_label} ({self.security_report.files_scanned} files scanned)",
            "Files:",
        ]
        for rel_path, content in sorted(self.manifest.files.items()):
            lines.append(f"  - {rel_path} ({len(content)} chars)")
        if self.security_report.findings:
            lines.append("Security Findings:")
            for finding in self.security_report.findings:
                lines.append(f"  ! {finding}")
        lines.append(
            f"Approve installation with approve_skill('{self.skill_id}') "
            f"or POST /api/v1/skills/approve/{self.skill_id}."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# URL & GitHub Source Parsing
# ---------------------------------------------------------------------------

def assert_safe_github_url(url: str) -> None:
    """
    Validate a URL via BrowserAgent.validate_url() and raise GuardrailViolation
    if SSRF validation fails.
    """
    import sys

    sm_mod = sys.modules.get("core.skill_manager")
    validator = getattr(sm_mod, "validate_url", validate_url) if sm_mod else validate_url
    res = validator(url)
    if isinstance(res, tuple):
        valid, reason = res
        if not valid:
            raise GuardrailViolation("ssrf_blocked", reason)


def sanitize_skill_name(raw_name: str) -> str:
    """Normalise a skill name to lowercase alphanumeric/hyphen/underscore."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw_name.strip().lower()).strip("-_")
    if not cleaned:
        cleaned = "custom-skill"
    cleaned = cleaned[:63]
    if not _SKILL_NAME_RE.match(cleaned):
        raise ValueError(f"Invalid skill name: {raw_name!r}")
    return cleaned


def validate_relative_skill_path(rel_path: str) -> str:
    """Ensure a file path inside a skill is strictly relative and free of traversal."""
    norm = rel_path.replace("\\", "/").lstrip("/")
    parts = [p for p in norm.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or ":" in norm:
        raise GuardrailViolation(
            "path_traversal",
            f"Unsafe relative path in skill archive: {rel_path!r}",
        )
    return "/".join(parts)


def parse_github_source(source: str) -> GitHubRef:
    """
    Parse a GitHub URL or 'owner/repo[/subpath]' shorthand into a GitHubRef.
    Enforces strict GitHub host allowlist and SSRF validation.
    """
    raw = source.strip()
    if not raw:
        raise ValueError("GitHub skill source cannot be empty.")

    if not raw.startswith(("http://", "https://", "ftp://", "file://")):
        if "://" in raw:
            raise GuardrailViolation("ssrf_scheme", f"Unsupported URL scheme in {raw!r}")
        parts = [p for p in raw.strip("/").split("/") if p]
        if len(parts) < 2:
            raise ValueError(f"Expected 'owner/repo' or full GitHub URL, got: {source!r}")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        subpath = "/".join(parts[2:]) if len(parts) > 2 else ""
        if subpath:
            subpath = validate_relative_skill_path(subpath)
        assert_safe_github_url(f"https://github.com/{owner}/{repo}")
        return GitHubRef(
            owner=owner,
            repo=repo,
            branch="main",
            subpath=subpath,
            is_single_file=subpath.endswith((".py", ".md")),
        )

    assert_safe_github_url(raw)
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_GITHUB_HOSTS:
        raise GuardrailViolation(
            "untrusted_skill_host",
            f"Host '{host}' is not in ALLOWED_GITHUB_HOSTS ({sorted(ALLOWED_GITHUB_HOSTS)}).",
        )

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if host == "raw.githubusercontent.com":
        if len(path_parts) < 4:
            raise ValueError(f"Invalid raw.githubusercontent.com URL: {raw!r}")
        owner, repo, branch = path_parts[0], path_parts[1], path_parts[2]
        subpath = validate_relative_skill_path("/".join(path_parts[3:]))
        return GitHubRef(owner=owner, repo=repo, branch=branch, subpath=subpath, is_single_file=True)

    if host == "api.github.com":
        if len(path_parts) >= 3 and path_parts[0] == "repos":
            owner, repo = path_parts[1], path_parts[2]
            subpath = (
                validate_relative_skill_path("/".join(path_parts[4:]))
                if len(path_parts) > 4 and path_parts[3] == "contents"
                else ""
            )
            return GitHubRef(
                owner=owner,
                repo=repo,
                branch="main",
                subpath=subpath,
                is_single_file=subpath.endswith((".py", ".md")),
            )
        raise ValueError(f"Unsupported api.github.com URL structure: {raw!r}")

    if len(path_parts) < 2:
        raise ValueError(f"GitHub URL must include owner and repo: {raw!r}")

    owner = path_parts[0]
    repo = path_parts[1].removesuffix(".git")
    branch, subpath, is_single_file = "main", "", False

    if len(path_parts) >= 4 and path_parts[2] in ("tree", "blob"):
        mode, branch = path_parts[2], path_parts[3]
        if len(path_parts) > 4:
            subpath = validate_relative_skill_path("/".join(path_parts[4:]))
        is_single_file = mode == "blob" or subpath.endswith((".py", ".md"))
    elif len(path_parts) > 2:
        subpath = validate_relative_skill_path("/".join(path_parts[2:]))
        is_single_file = subpath.endswith((".py", ".md"))

    return GitHubRef(owner=owner, repo=repo, branch=branch, subpath=subpath, is_single_file=is_single_file)


# ---------------------------------------------------------------------------
# Static AST & Content Security Scanner
# ---------------------------------------------------------------------------

def scan_python_ast(rel_path: str, code: str) -> list[str]:
    """Parse Python code into an AST and return a list of security findings."""
    findings: list[str] = []
    try:
        tree = ast.parse(code, filename=rel_path)
    except SyntaxError as exc:
        return [f"{rel_path}: SyntaxError at line {exc.lineno}: {exc.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BLOCKED_AST_MODULES:
                    findings.append(f"{rel_path}:{node.lineno}: Blocked import '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _BLOCKED_AST_MODULES:
                findings.append(f"{rel_path}:{node.lineno}: Blocked import from '{node.module}'")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_AST_CALLS:
                findings.append(f"{rel_path}:{node.lineno}: Blocked builtin call '{node.func.id}()'")
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                pair = (node.func.value.id, node.func.attr)
                if pair in _BLOCKED_AST_ATTRS:
                    findings.append(f"{rel_path}:{node.lineno}: Blocked call '{pair[0]}.{pair[1]}()'")
    return findings


def scan_skill_files(files: dict[str, str]) -> SkillSecurityReport:
    """Validate file counts, sizes, relative paths, and Python AST safety."""
    findings: list[str] = []
    has_python = False
    if not files:
        return SkillSecurityReport(passed=False, permission_level=1, findings=["Skill contains no files."], files_scanned=0)
    if len(files) > MAX_FILES_PER_SKILL:
        findings.append(f"Skill has {len(files)} files, exceeding MAX_FILES_PER_SKILL ({MAX_FILES_PER_SKILL}).")

    for rel_path, content in files.items():
        try:
            validate_relative_skill_path(rel_path)
        except GuardrailViolation as exc:
            findings.append(str(exc))
            continue
        byte_len = len(content.encode("utf-8"))
        if byte_len > MAX_FILE_BYTES:
            findings.append(f"{rel_path}: size {byte_len} bytes exceeds {MAX_FILE_BYTES} byte limit.")
        if rel_path.endswith(".py"):
            has_python = True
            findings.extend(scan_python_ast(rel_path, content))

    return SkillSecurityReport(
        passed=len(findings) == 0,
        permission_level=2 if has_python else 1,
        findings=findings,
        files_scanned=len(files),
    )


def parse_skill_md(content: str, fallback_name: str = "github-skill") -> tuple[str, str, list[str], str]:
    """Parse YAML frontmatter and markdown instructions from a SKILL.md file."""
    stripped = content.strip()
    name, description, tags, body = fallback_name, f"Open-source skill '{fallback_name}'", [], stripped
    if stripped.startswith("---"):
        parts = stripped.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()
            try:
                meta = yaml.safe_load(parts[1].strip()) or {}
                if isinstance(meta, dict):
                    name = sanitize_skill_name(str(meta.get("name") or fallback_name))
                    description = str(meta.get("description") or description).strip()
                    if isinstance(meta.get("tags"), list):
                        tags = [str(t) for t in meta["tags"]]
            except Exception as exc:
                logfire.warn("parse_skill_md: YAML frontmatter warning", error=str(exc))
    return sanitize_skill_name(name), description, tags, body


def synthesize_skill_md_from_python(
    skill_name: str, source_url: str, py_files: dict[str, str]
) -> tuple[str, str]:
    """Inspect Python files via AST to synthesize a SKILL.md for open-source Python tools."""
    summaries: list[str] = []
    functions: list[str] = []
    for _rel_path, code in sorted(py_files.items()):
        try:
            tree = ast.parse(code)
            mod_doc = ast.get_docstring(tree)
            if mod_doc:
                summaries.append(mod_doc.strip().splitlines()[0])
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                    fn_doc = (ast.get_docstring(node) or "").strip().splitlines()
                    functions.append(f"- `{node.name}()`: {fn_doc[0] if fn_doc else 'No docstring'}")
        except SyntaxError:
            continue

    description = summaries[0] if summaries else f"Open-source Python skill adapted from {source_url}"
    fn_section = "\n".join(functions) if functions else "- Executable script"
    skill_md = (
        f"---\nname: {skill_name}\ndescription: {description}\n"
        f"tags: [open-source, github, python-tool]\n---\n\n"
        f"# {skill_name}\n\nAdapted from `{source_url}`.\n\n"
        f"## Exposed Functions & Entrypoints\n{fn_section}\n"
    )
    return description, skill_md
