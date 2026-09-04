"""
Yaazhi V4 — Cognitive Router & Classifier.

Implements the domain router described in the V4 framework:
  - Extracts Mutarporuḷ parameters (Tiṇai / Kālam) from user input.
  - Classifies requests as Akam (private/internal) or Puram (public/execution).
  - Returns a RouteDecision that the orchestrator consumes before context assembly.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

import logfire
from pydantic import BaseModel, Field


# ─── Tiṇai (Environmental Domain) ────────────────────────────────────────────

class TinaiCategory(str, Enum):
    """
    Classical Tiṇai categories mapped to modern dev domains (Mutarporuḷ Space).

    In Tamil Sangam poetics, Tiṇai describes the landscape / emotional register
    of a poem.  Here each value represents a discrete execution context (domain)
    so that memory retrieval, agent selection, and tone can be Tiṇai-pre-filtered.
    """
    ARCH_LINUX_DEV     = "arch_linux_dev"
    BMS_ML_PROJECT     = "bms_ml_project"
    CAMPUS_BOOKING_APP = "campus_booking_app"
    GENERAL_TECHNICAL  = "general_technical"
    PERSONAL_IDENTITY  = "personal_identity"


# ─── Kālam (Temporal Epoch) ───────────────────────────────────────────────────

class KalamEpoch(str, Enum):
    """Current recognised temporal phases of the user's life context."""
    UNIVERSITY_BUILDER = "University_Builder_Epoch"
    EARLY_CAREER       = "Early_Career_Epoch"
    RESEARCH_SPRINT    = "Research_Sprint_Epoch"


# ─── Domain (Akam / Puram) ────────────────────────────────────────────────────

class Domain(str, Enum):
    """
    Binary classification that maps to the Akam/Puram structural division.

    Akam  — private, internal state (identity, affect, preferences, meta-learning).
    Puram — public execution space (code, research, browsing, episodic events).
    """
    AKAM  = "akam"   # Layers 4, 7, 11, 12
    PURAM = "puram"  # Layers 1, 2, 5, 6


# ─── Keyword Signals ──────────────────────────────────────────────────────────

_AKAM_SIGNALS: frozenset[str] = frozenset({
    "burnout", "stress", "prefer", "preference", "identity", "who am i",
    "how am i feeling", "my values", "my goals", "constraint", "local only",
    "private", "reflect", "introspect", "motivation", "mood", "emotion",
    "personal", "myself", "remind me who", "my character",
})

_PURAM_SIGNALS: frozenset[str] = frozenset({
    "code", "write", "implement", "fix", "debug", "research", "search",
    "browse", "navigate", "read", "summarise", "summarize", "email", "notify",
    "alert", "run", "execute", "test", "build", "deploy", "analyse", "analyze",
    "fetch", "download", "schedule", "task", "plan", "remember", "what is",
    "explain", "show", "list", "find", "create",
})

_TINAI_KEYWORD_MAP: dict[TinaiCategory, frozenset[str]] = {
    TinaiCategory.ARCH_LINUX_DEV: frozenset({
        "arch", "linux", "pacman", "systemd", "kernel", "bash", "zsh",
        "terminal", "shell", "dotfile", "nvim", "neovim", "wayland", "xorg",
        "compositor", "hyprland", "sway",
    }),
    TinaiCategory.BMS_ML_PROJECT: frozenset({
        "bms", "battery", "ml", "machine learning", "model", "training",
        "inference", "dataset", "pytorch", "tensorflow", "sklearn", "xgboost",
        "soc", "state of charge", "degradation", "cell",
    }),
    TinaiCategory.CAMPUS_BOOKING_APP: frozenset({
        "campus", "booking", "room", "slot", "reservation", "schedule",
        "timetable", "college", "university", "facility",
    }),
    TinaiCategory.PERSONAL_IDENTITY: frozenset({
        "identity", "who am i", "my goals", "my values", "preference",
        "burnout", "personal", "myself", "introspect",
    }),
}


# ─── Route Decision Model ─────────────────────────────────────────────────────

class RouteDecision(BaseModel):
    """
    Result of the cognitive router's classification pass.

    Consumed by the orchestrator before context assembly begins.
    """
    domain:      Domain        = Field(..., description="Akam or Puram routing")
    tinai:       TinaiCategory = Field(..., description="Active execution domain")
    kalam_epoch: KalamEpoch   = Field(default=KalamEpoch.UNIVERSITY_BUILDER)
    akam_layers: list[int]    = Field(default_factory=list,  description="Akam memory layers to query")
    puram_layers: list[int]   = Field(default_factory=list,  description="Puram memory layers to query")
    confidence:  float        = Field(default=0.8, ge=0.0, le=1.0)
    reasoning:   str          = Field(default="")

    class Config:
        use_enum_values = True


# ─── Cognitive Router ─────────────────────────────────────────────────────────

class CognitiveRouter:
    """
    Stateless classifier that routes a user prompt to the correct memory domain
    and extracts Mutarporuḷ parameters (Tiṇai + Kālam) for downstream filtering.

    Usage::

        router = CognitiveRouter()
        decision = router.classify("fix my arch linux suspend script")
        # decision.domain == Domain.PURAM
        # decision.tinai  == TinaiCategory.ARCH_LINUX_DEV
    """

    # Akam layers (private store)
    _AKAM_LAYERS:  list[int] = [4, 7, 11, 12]
    # Puram layers (execution store)
    _PURAM_LAYERS: list[int] = [1, 2, 5, 6]

    def classify(
        self,
        user_input: str,
        kalam_epoch: Optional[KalamEpoch] = None,
    ) -> RouteDecision:
        """
        Classify *user_input* and return a RouteDecision.

        Args:
            user_input:  Raw user message (pre-sanitisation).
            kalam_epoch: Override the active temporal epoch. If None, uses the
                         default University_Builder_Epoch.

        Returns:
            RouteDecision with domain, tinai, kalam, and layer lists populated.
        """
        text_lower = user_input.lower()
        epoch = kalam_epoch or KalamEpoch.UNIVERSITY_BUILDER

        domain, domain_conf, domain_reason = self._detect_domain(text_lower)
        tinai, tinai_conf = self._detect_tinai(text_lower)

        overall_conf = round((domain_conf + tinai_conf) / 2, 3)

        decision = RouteDecision(
            domain=domain,
            tinai=tinai,
            kalam_epoch=epoch,
            akam_layers=self._AKAM_LAYERS if domain == Domain.AKAM else [],
            puram_layers=self._PURAM_LAYERS if domain == Domain.PURAM else [],
            confidence=overall_conf,
            reasoning=domain_reason,
        )

        logfire.info(
            "CognitiveRouter.classify",
            domain=decision.domain,
            tinai=decision.tinai,
            confidence=decision.confidence,
            input_preview=user_input[:60],
        )
        return decision

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _detect_domain(self, text: str) -> tuple[Domain, float, str]:
        """Keyword-score the text against Akam / Puram signal sets."""
        akam_hits  = sum(1 for kw in _AKAM_SIGNALS  if kw in text)
        puram_hits = sum(1 for kw in _PURAM_SIGNALS if kw in text)

        if akam_hits == 0 and puram_hits == 0:
            return Domain.PURAM, 0.6, "No strong signals — defaulting to Puram"

        total = akam_hits + puram_hits
        if akam_hits > puram_hits:
            conf = min(0.95, 0.6 + (akam_hits / total) * 0.4)
            return Domain.AKAM, conf, f"Akam signals: {akam_hits} vs Puram: {puram_hits}"
        else:
            conf = min(0.95, 0.6 + (puram_hits / total) * 0.4)
            return Domain.PURAM, conf, f"Puram signals: {puram_hits} vs Akam: {akam_hits}"

    def _detect_tinai(self, text: str) -> tuple[TinaiCategory, float]:
        """Return the Tiṇai with the highest keyword hit count."""
        scores: dict[TinaiCategory, int] = {}
        for category, keywords in _TINAI_KEYWORD_MAP.items():
            scores[category] = sum(1 for kw in keywords if kw in text)

        # If no hits, default to general_technical
        if max(scores.values()) == 0:
            return TinaiCategory.GENERAL_TECHNICAL, 0.5

        best = max(scores, key=lambda k: scores[k])
        total_hits = sum(scores.values())
        conf = min(0.95, 0.5 + (scores[best] / max(total_hits, 1)) * 0.45)
        return best, conf


# ─── Module-level singleton ───────────────────────────────────────────────────
cognitive_router = CognitiveRouter()
