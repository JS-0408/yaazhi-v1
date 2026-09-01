"""
memory_viewer.py — Yaazhi Dashboard Memory Viewer Component
Displays stored memories, allows semantic search, and lets the user delete or tag entries.
"""

import streamlit as st
from datetime import datetime
from typing import Optional


# ─────────────────────────────────────────────────────────
# Safe import: only fails gracefully if memory layer is absent
# ─────────────────────────────────────────────────────────
try:
    from memory.retriever import MemoryRetriever
    from memory.vector_store import VectorStore
    _MEMORY_AVAILABLE = True
except ImportError:
    _MEMORY_AVAILABLE = False


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _format_timestamp(ts: Optional[str]) -> str:
    """Convert ISO timestamp to a human-readable string."""
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d %b %Y · %I:%M %p")
    except ValueError:
        return ts


def _badge(label: str, color: str = "#3b82f6") -> str:
    """Return an HTML badge string for use with st.markdown."""
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:999px;font-size:0.75rem;font-weight:600;">{label}</span>'
    )


# ─────────────────────────────────────────────────────────
# Main component
# ─────────────────────────────────────────────────────────

def render_memory_viewer() -> None:
    """Render the full memory viewer panel inside the Streamlit dashboard."""

    st.markdown("## 🧠 Memory Vault")
    st.caption("Browse, search, and manage Yaazhi's long-term semantic memory.")

    if not _MEMORY_AVAILABLE:
        st.warning(
            "⚠️  Memory layer not initialised.  "
            "Make sure ChromaDB / pgvector is running and `memory/` packages are installed."
        )
        _render_demo_memories()
        return

    # ── Toolbar ──────────────────────────────────────────
    col_search, col_filter, col_refresh = st.columns([4, 2, 1])

    with col_search:
        query = st.text_input(
            "🔍 Semantic search",
            placeholder="e.g. 'ESP32 MQTT setup', 'Fourier transform notes'…",
            key="mem_search_query",
        )

    with col_filter:
        category = st.selectbox(
            "Category",
            ["All", "Conversation", "Document", "Task", "Preference"],
            key="mem_category_filter",
        )

    with col_refresh:
        st.markdown("<br>", unsafe_allow_html=True)
        refresh = st.button("⟳", help="Refresh memory list", key="mem_refresh_btn")

    st.divider()

    # ── Fetch memories ────────────────────────────────────
    retriever = MemoryRetriever()

    try:
        if query.strip():
            results = retriever.search(
                query=query,
                top_k=20,
                category=None if category == "All" else category,
            )
        else:
            results = retriever.list_recent(
                limit=50,
                category=None if category == "All" else category,
            )
    except Exception as exc:
        st.error(f"Failed to fetch memories: {exc}")
        return

    if not results:
        st.info("No memories found. Start chatting with Yaazhi to build the vault!")
        return

    # ── Stats bar ─────────────────────────────────────────
    st.markdown(
        f"**{len(results)}** memor{'y' if len(results) == 1 else 'ies'} "
        f"{'matching **' + query + '**' if query.strip() else 'stored'}",
        unsafe_allow_html=True,
    )

    # ── Memory cards ─────────────────────────────────────
    for i, mem in enumerate(results):
        _render_memory_card(mem, index=i)


# ─────────────────────────────────────────────────────────
# Sub-components
# ─────────────────────────────────────────────────────────

def _render_memory_card(memory: dict, index: int) -> None:
    """Render a single memory entry as an expandable card."""

    category   = memory.get("category", "General")
    content    = memory.get("content", "")
    timestamp  = _format_timestamp(memory.get("timestamp"))
    mem_id     = memory.get("id", f"mem_{index}")
    score      = memory.get("score")          # similarity score (optional)
    tags       = memory.get("tags", [])

    # Colour map for category badges
    cat_colors = {
        "Conversation": "#6366f1",
        "Document":     "#0ea5e9",
        "Task":         "#f59e0b",
        "Preference":   "#10b981",
    }
    cat_color = cat_colors.get(category, "#64748b")

    # Build header label
    preview = content[:80] + "…" if len(content) > 80 else content
    header  = f"{_badge(category, cat_color)}&nbsp; {preview}"

    with st.expander(label="", expanded=False):
        # Re-render badge inside expander since label doesn't support HTML
        st.markdown(
            f"{_badge(category, cat_color)}&nbsp;&nbsp;"
            + (f"**Score:** `{score:.3f}`" if score is not None else ""),
            unsafe_allow_html=True,
        )
        st.markdown(f"**🕐 {timestamp}**")
        st.markdown("---")
        st.write(content)

        if tags:
            tag_html = " ".join(_badge(t, "#64748b") for t in tags)
            st.markdown(f"**Tags:** {tag_html}", unsafe_allow_html=True)

        # Action row
        col_del, col_tag, _ = st.columns([1, 1, 4])
        with col_del:
            if st.button("🗑 Delete", key=f"del_{mem_id}"):
                _delete_memory(mem_id)
        with col_tag:
            new_tag = st.text_input("Add tag", key=f"tag_input_{mem_id}", label_visibility="collapsed",
                                    placeholder="Add tag…")
            if new_tag:
                _add_tag(mem_id, new_tag)


def _render_demo_memories() -> None:
    """Show placeholder demo cards when memory layer is offline."""

    st.markdown("### 📋 Demo Memories (memory layer offline)")
    demo = [
        {
            "category": "Conversation",
            "content": "Santhosh asked about LangGraph stateful agents and their use in multi-agent orchestration.",
            "timestamp": "2026-05-09T10:30:00",
            "score": 0.941,
            "tags": ["langgraph", "agents"],
        },
        {
            "category": "Document",
            "content": "ECE Semester 4 — Digital Signal Processing lecture notes uploaded and indexed. 214 pages.",
            "timestamp": "2026-05-08T18:00:00",
            "score": 0.876,
            "tags": ["dsp", "btech"],
        },
        {
            "category": "Preference",
            "content": "Preferred response language: Telugu. Preferred notification time: 8 AM IST.",
            "timestamp": "2026-05-07T09:15:00",
            "score": 0.812,
            "tags": ["preferences"],
        },
    ]

    for i, mem in enumerate(demo):
        with st.expander(f"{mem['category']} · {mem['content'][:60]}…", expanded=i == 0):
            st.write(mem["content"])
            st.caption(f"🕐 {_format_timestamp(mem['timestamp'])}  ·  Score: {mem['score']:.3f}")
            st.markdown(
                " ".join(f"`{t}`" for t in mem["tags"])
            )


# ─────────────────────────────────────────────────────────
# Action handlers
# ─────────────────────────────────────────────────────────

def _delete_memory(mem_id: str) -> None:
    """Delete a memory entry by ID."""
    if not _MEMORY_AVAILABLE:
        st.toast(f"[Demo] Would delete memory {mem_id}", icon="🗑")
        return
    try:
        vs = VectorStore()
        vs.delete(mem_id)
        st.toast("Memory deleted.", icon="✅")
        st.rerun()
    except Exception as exc:
        st.error(f"Could not delete memory: {exc}")


def _add_tag(mem_id: str, tag: str) -> None:
    """Append a tag to a memory entry."""
    if not _MEMORY_AVAILABLE:
        st.toast(f"[Demo] Would tag memory {mem_id} with '{tag}'", icon="🏷")
        return
    try:
        vs = VectorStore()
        vs.add_tag(mem_id, tag)
        st.toast(f"Tag '{tag}' added.", icon="🏷")
        st.rerun()
    except Exception as exc:
        st.error(f"Could not add tag: {exc}")
