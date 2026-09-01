"""
task_queue.py — Yaazhi Dashboard Task Queue Component
Displays active, pending, and completed agent tasks with live status polling.
"""

import time
import streamlit as st
from datetime import datetime
from typing import Any, Optional


# ─────────────────────────────────────────────────────────
# Safe import of core orchestrator state
# ─────────────────────────────────────────────────────────
try:
    from core.state import YaazhiState
    from core.orchestrator import YaazhiOrchestrator
    _CORE_AVAILABLE = True
except ImportError:
    _CORE_AVAILABLE = False


# ─────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────

STATUS_ICONS = {
    "pending":    "⏳",
    "running":    "🔄",
    "completed":  "✅",
    "failed":     "❌",
    "cancelled":  "🚫",
    "reviewing":  "🔍",
}

STATUS_COLORS = {
    "pending":    "#f59e0b",
    "running":    "#3b82f6",
    "completed":  "#10b981",
    "failed":     "#ef4444",
    "cancelled":  "#6b7280",
    "reviewing":  "#8b5cf6",
}

PRIORITY_LABELS = {1: "🔴 Critical", 2: "🟠 High", 3: "🟡 Normal", 4: "🟢 Low"}


# ─────────────────────────────────────────────────────────
# Main component
# ─────────────────────────────────────────────────────────

def render_task_queue() -> None:
    """Render the live task queue panel in the Streamlit dashboard."""

    st.markdown("## 🗂 Agent Task Queue")
    st.caption("Live view of all tasks Yaazhi is running, reviewing, or has completed.")

    # ── Header controls ──────────────────────────────────
    col_filter, col_auto, col_clear = st.columns([3, 2, 2])

    with col_filter:
        status_filter = st.multiselect(
            "Show statuses",
            options=["pending", "running", "reviewing", "completed", "failed", "cancelled"],
            default=["pending", "running", "reviewing"],
            key="tq_status_filter",
        )

    with col_auto:
        auto_refresh = st.toggle("Auto-refresh (5s)", value=False, key="tq_auto_refresh")

    with col_clear:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑 Clear completed", key="tq_clear_done"):
            _clear_completed_tasks()

    st.divider()

    # ── Auto-refresh loop ─────────────────────────────────
    placeholder = st.empty()

    if auto_refresh:
        while True:
            with placeholder.container():
                _render_queue(status_filter)
            time.sleep(5)
            st.rerun()
    else:
        with placeholder.container():
            _render_queue(status_filter)


# ─────────────────────────────────────────────────────────
# Inner render
# ─────────────────────────────────────────────────────────

def _render_queue(status_filter: list[str]) -> None:
    """Fetch tasks and render each card."""

    tasks = _fetch_tasks(status_filter)

    if not tasks:
        st.info("No tasks match the selected filters. Ask Yaazhi something to generate tasks!")
        return

    # Summary metrics
    all_tasks = _fetch_tasks(list(STATUS_ICONS.keys()))
    running   = sum(1 for t in all_tasks if t.get("status") == "running")
    pending   = sum(1 for t in all_tasks if t.get("status") == "pending")
    done      = sum(1 for t in all_tasks if t.get("status") == "completed")
    failed    = sum(1 for t in all_tasks if t.get("status") == "failed")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🔄 Running",   running)
    m2.metric("⏳ Pending",   pending)
    m3.metric("✅ Completed", done)
    m4.metric("❌ Failed",    failed)

    st.markdown("---")

    for task in tasks:
        _render_task_card(task)


def _render_task_card(task: dict) -> None:
    """Render a single task as a styled expandable card."""

    task_id    = task.get("id", "unknown")
    title      = task.get("title", "Unnamed Task")
    status     = task.get("status", "pending")
    agent      = task.get("agent", "Orchestrator")
    priority   = task.get("priority", 3)
    progress   = task.get("progress", 0)           # 0–100
    created_at = task.get("created_at", "")
    updated_at = task.get("updated_at", "")
    error_msg  = task.get("error")
    sub_tasks  = task.get("sub_tasks", [])
    result_preview = task.get("result_preview", "")

    icon  = STATUS_ICONS.get(status, "❓")
    color = STATUS_COLORS.get(status, "#64748b")
    prio_label = PRIORITY_LABELS.get(priority, "Normal")

    header = f"{icon} **{title}**"

    with st.expander(header, expanded=(status in ("running", "failed"))):
        # Top info row
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**Agent:** `{agent}`")
        c2.markdown(f"**Priority:** {prio_label}")
        c3.markdown(
            f"<span style='color:{color};font-weight:700'>{status.upper()}</span>",
            unsafe_allow_html=True,
        )

        # Progress bar for running tasks
        if status == "running" and progress > 0:
            st.progress(progress / 100, text=f"{progress}% complete")

        # Timestamps
        if created_at:
            st.caption(f"🕐 Created: {_fmt_ts(created_at)}")
        if updated_at:
            st.caption(f"🔄 Updated: {_fmt_ts(updated_at)}")

        # Sub-task breakdown
        if sub_tasks:
            st.markdown("**Sub-tasks:**")
            for st_item in sub_tasks:
                st_icon  = STATUS_ICONS.get(st_item.get("status", "pending"), "❓")
                st_title = st_item.get("title", "Sub-task")
                st.markdown(f"&nbsp;&nbsp;{st_icon} {st_title}")

        # Error detail
        if error_msg:
            st.error(f"**Error:** {error_msg}")

        # Result preview
        if result_preview:
            with st.container():
                st.markdown("**Result preview:**")
                st.code(result_preview[:500], language="text")

        # Action buttons
        btn_cols = st.columns([1, 1, 4])
        with btn_cols[0]:
            if status in ("pending", "running") and st.button("🚫 Cancel", key=f"cancel_{task_id}"):
                _cancel_task(task_id)
        with btn_cols[1]:
            if status == "failed" and st.button("♻️ Retry", key=f"retry_{task_id}"):
                _retry_task(task_id)


# ─────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────

def _fetch_tasks(status_filter: list[str]) -> list[dict]:
    """Return task list from orchestrator or demo data."""

    if _CORE_AVAILABLE:
        try:
            orch = YaazhiOrchestrator()
            tasks = orch.get_task_queue()
            return [t for t in tasks if t.get("status") in status_filter]
        except Exception:
            pass  # Fall through to demo

    # Demo data when core is offline
    now = datetime.utcnow().isoformat()
    demo_tasks = [
        {
            "id": "task_001",
            "title": "Research: Best free LLM APIs for Indian students in 2026",
            "status": "completed",
            "agent": "Researcher",
            "priority": 2,
            "progress": 100,
            "created_at": "2026-05-09T09:00:00",
            "updated_at": "2026-05-09T09:04:22",
            "result_preview": "Top options: Groq (free, 30k TPM), Together AI (free tier), Gemini 1.5 Flash (free)…",
            "sub_tasks": [
                {"title": "Web search: Groq pricing", "status": "completed"},
                {"title": "Web search: Together AI free tier", "status": "completed"},
                {"title": "Summarise findings", "status": "completed"},
            ],
        },
        {
            "id": "task_002",
            "title": "Write Python script: auto-send email digest every morning",
            "status": "running",
            "agent": "Coder",
            "priority": 2,
            "progress": 60,
            "created_at": "2026-05-09T10:00:00",
            "updated_at": now,
            "sub_tasks": [
                {"title": "Draft email template", "status": "completed"},
                {"title": "Write SMTP handler", "status": "running"},
                {"title": "Test with dummy data", "status": "pending"},
            ],
        },
        {
            "id": "task_003",
            "title": "Index ECE Semester 4 notes into memory vault",
            "status": "pending",
            "agent": "Reader",
            "priority": 3,
            "progress": 0,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "task_004",
            "title": "Send WhatsApp notification: DSP assignment due tomorrow",
            "status": "failed",
            "agent": "Notifier",
            "priority": 1,
            "progress": 0,
            "created_at": "2026-05-09T08:00:00",
            "updated_at": "2026-05-09T08:01:05",
            "error": "n8n webhook returned 503. Retry with exponential back-off.",
        },
    ]
    return [t for t in demo_tasks if t.get("status") in status_filter]


def _fmt_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%d %b %Y · %I:%M %p")
    except Exception:
        return ts


# ─────────────────────────────────────────────────────────
# Action handlers
# ─────────────────────────────────────────────────────────

def _cancel_task(task_id: str) -> None:
    if _CORE_AVAILABLE:
        try:
            YaazhiOrchestrator().cancel_task(task_id)
        except Exception as exc:
            st.error(f"Cancel failed: {exc}")
            return
    st.toast(f"Task {task_id} cancelled.", icon="🚫")
    st.rerun()


def _retry_task(task_id: str) -> None:
    if _CORE_AVAILABLE:
        try:
            YaazhiOrchestrator().retry_task(task_id)
        except Exception as exc:
            st.error(f"Retry failed: {exc}")
            return
    st.toast(f"Task {task_id} queued for retry.", icon="♻️")
    st.rerun()


def _clear_completed_tasks() -> None:
    if _CORE_AVAILABLE:
        try:
            YaazhiOrchestrator().clear_completed()
        except Exception as exc:
            st.error(f"Clear failed: {exc}")
            return
    st.toast("Completed tasks cleared.", icon="🗑")
    st.rerun()
