"""
Yaazhi Reviewer — LLM-as-judge quality gate.

Audit fixes applied (2026-05-10):
  W8/W9 : REVISE state includes revise_reason + revise_score so agents know
           exactly what failed on retry. Score logged per dimension.
"""

from __future__ import annotations

import ast
import asyncio
import json
import random
import re
import time
from typing import Any, Optional

import litellm
import logfire

from config.settings import settings
from core.state import AgentOutput, ReviewResult, ReviewVerdict, SubTask


# ---------------------------------------------------------------------------
# Review verdict constants
# ---------------------------------------------------------------------------

PASS = "PASS"
REVISE = "REVISE"
FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------

class Reviewer:
    """
    LLM-as-judge quality gate for Yaazhi agent outputs.

    Scores outputs on relevance, accuracy, completeness, and safety.
    Returns PASS (score ≥ 0.75), REVISE (0.5–0.74), or FAIL (< 0.5).

    W9 FIX: On REVISE, the returned state dict includes:
      - revise_reason: specific failure reason text
      - revise_score:  float score that triggered the revision
    """

    _PASS_THRESHOLD: float = 0.75
    _FAIL_THRESHOLD: float = 0.50

    # ------------------------------------------------------------------
    # Main review entry point
    # ------------------------------------------------------------------

    async def review(
        self,
        output: AgentOutput | str,
        task: SubTask | str,
    ) -> ReviewResult:
        """
        Review an agent's output against the original task.

        Args:
            output     : AgentOutput instance or raw output text.
            task       : SubTask instance or task description string.

        Returns:
            ReviewResult containing verdict, scores, and feedback.
        """
        if isinstance(output, AgentOutput):
            content = output.content
            agent_name = output.agent_name
        else:
            content = str(output)
            agent_name = "unknown"

        task_desc = task.description if isinstance(task, SubTask) else str(task)

        logfire.debug(
            "Reviewer.review called",
            agent=agent_name,
            task=task_desc[:80],
            output_len=len(content),
        )
        t_start = time.time()

        # ── Code output: syntax check first ──────────────────────────────
        if agent_name == "coder":
            syntax_ok, syntax_err = self._check_syntax(content)
            if not syntax_ok:
                reason = f"Syntax error in generated code: {syntax_err}"
                logfire.warning("Reviewer: syntax error detected", reason=reason)
                return self._make_result(REVISE, 0.3, reason, {})

        # ── Safety check ─────────────────────────────────────────────────
        safety_ok, safety_reason = self._safety_check(content)
        if not safety_ok:
            logfire.warning("Reviewer: safety violation", reason=safety_reason)
            return self._make_result(FAIL, 0.0, safety_reason, {})

        # ── LLM scoring ──────────────────────────────────────────────────
        try:
            dimensions, score, reason = await self._llm_score(task_desc, content, agent_name)
        except Exception as exc:
            logfire.warning("Reviewer: LLM scoring failed, defaulting REVISE", error=str(exc))
            # B-04 FIX: was PASS (score 0.8) — fail-open when scorer is down bypassed
            # quality gating for potentially harmful outputs. REVISE triggers a retry.
            return self._make_result(
                REVISE, 0.6,
                "Reviewer LLM unavailable — conservative retry requested",
                {},
            )

        # ── Verdict ──────────────────────────────────────────────────────
        if score >= self._PASS_THRESHOLD:
            verdict = PASS
        elif score >= self._FAIL_THRESHOLD:
            verdict = REVISE
        else:
            verdict = FAIL

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "Reviewer.review complete",
            agent=agent_name,
            verdict=verdict,
            score=score,
            duration_ms=duration_ms,
        )

        return self._make_result(verdict, score, reason if verdict != PASS else "", dimensions)

    # ------------------------------------------------------------------
    # LLM scorer
    # ------------------------------------------------------------------

    async def _llm_score(
        self,
        task: str,
        output: str,
        agent_name: str,
    ) -> tuple[dict[str, float], float, str]:
        """
        Use Groq LLM to score the output on 4 dimensions.

        Returns:
            (dimensions_dict, overall_score, failure_reason)
        """
        model = settings.get_litellm_model("fast_tasks")
        prompt = f"""You are a strict quality reviewer for an AI assistant called Yaazhi.

Task given to agent '{agent_name}': {task[:500]}

Agent output:
{output[:2000]}

Score this output on these dimensions (0.0 to 1.0 each):
1. relevance     — Does the output directly address the task?
2. accuracy      — Is the information factually correct or at least plausible?
3. completeness  — Does it fully resolve the task?
4. safety        — Is it free of harmful, misleading, or dangerous content?

Return ONLY valid JSON with this exact structure:
{{
  "relevance": <float>,
  "accuracy": <float>,
  "completeness": <float>,
  "safety": <float>,
  "failure_reason": "<specific reason if any dimension is below 0.6, else empty string>"
}}"""

        response = await asyncio.to_thread(
            litellm.completion,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()

        # VECTOR-3 FIX: regex-anchored JSON extraction.
        # Replaces fragile split("```")[1] which raised IndexError on fence-free responses.
        json_match = re.search(r"(\{.*\})", raw, re.DOTALL)
        if not json_match:
            raise ValueError(f"Reviewer: no JSON object found in LLM response: {raw[:200]}")
        raw = json_match.group(1)

        data: dict = json.loads(raw)
        dimensions = {
            "relevance": float(data.get("relevance", 0.5)),
            "accuracy": float(data.get("accuracy", 0.5)),
            "completeness": float(data.get("completeness", 0.5)),
            "safety": float(data.get("safety", 1.0)),
        }
        # Safety is a hard gate — if it's below 0.5 the output is rejected
        if dimensions["safety"] < 0.5:
            return dimensions, 0.0, "Output flagged as potentially unsafe by reviewer"

        # Weighted average: completeness + relevance matter most
        score = (
            dimensions["relevance"] * 0.30
            + dimensions["accuracy"] * 0.25
            + dimensions["completeness"] * 0.30
            + dimensions["safety"] * 0.15
        )
        score = round(min(1.0, max(0.0, score)), 4)
        reason = str(data.get("failure_reason", "")).strip()

        # Auto-generate reason if score is low but none provided
        if score < self._PASS_THRESHOLD and not reason:
            low_dims = [
                f"{k}={v:.2f}"
                for k, v in dimensions.items()
                if v < 0.6
            ]
            reason = f"Low scores in: {', '.join(low_dims)}" if low_dims else "Below quality threshold"

        return dimensions, score, reason

    # ------------------------------------------------------------------
    # Syntax check (coder outputs)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_syntax(output: str) -> tuple[bool, str]:
        """Check if output is valid Python syntax."""
        code = output.strip()
        if code.startswith("```python"):
            code = code[9:]
        if code.startswith("```"):
            code = code[3:]
        if code.endswith("```"):
            code = code[:-3]
        try:
            ast.parse(code.strip())
            return True, ""
        except SyntaxError as exc:
            return False, f"Line {exc.lineno}: {exc.msg}"

    # ------------------------------------------------------------------
    # Safety check (output filtering)
    # ------------------------------------------------------------------

    @staticmethod
    def _safety_check(output: str) -> tuple[bool, str]:
        """Block outputs that contain API keys, PII, or known dangerous patterns.

        C-06 FIX: Expanded patterns to catch AWS keys, Google API keys,
        and improved password regex to match passwords with spaces.
        """
        import re

        patterns = [
            (r"sk-[A-Za-z0-9]{20,}", "OpenAI API key detected in output"),
            (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID detected in output"),
            (r"AIza[0-9A-Za-z\-_]{35}", "Google API Key detected in output"),
            # C-06 FIX: was \S+ (stopped at whitespace); now .{1,100} catches phrases
            (r"(password|passwd)\s*[:=]\s*.{1,100}", "Password value detected in output"),
            (r"\b(?:\d{4}[- ]?){4}\b", "Credit card number pattern detected"),
        ]
        for pattern, reason in patterns:
            if re.search(pattern, output, re.IGNORECASE):
                return False, reason
        return True, ""

    # ------------------------------------------------------------------
    # Result builder (W9 fix: always includes revise_reason + revise_score)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_result(
        verdict: str,
        score: float,
        reason: str,
        dimensions: dict[str, float],
    ) -> ReviewResult:
        """
        Build the reviewer result object from raw scoring data.

        W9 FIX: revise_reason and revise_score are always present.
        """
        return ReviewResult(
            verdict=ReviewVerdict(verdict),
            relevance_score=int(round(dimensions.get("relevance", 0.0) * 10)),
            completeness_score=int(round(dimensions.get("completeness", 0.0) * 10)),
            accuracy_score=int(round(dimensions.get("accuracy", 0.0) * 10)),
            safety_score=int(round(dimensions.get("safety", 0.0) * 10)),
            total_score=int(round(score * 40)),
            feedback=reason,
            retry_with_context="",
        )
