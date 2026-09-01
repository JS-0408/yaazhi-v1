"""
Yaazhi Streamlit Dashboard.

5-page dashboard with sidebar navigation:
  💬 Chat    — AI conversation with voice input/output
  🧠 Memory  — Search, browse, and add memories
  📁 Knowledge — Upload and manage document knowledge base
  📊 System  — Live service health and Grafana
  ⚙️ Settings — Model config, API key, preferences

All API calls go to the Yaazhi FastAPI backend via httpx.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

import httpx
import streamlit as st

from config.settings import settings

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Yaazhi OS",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"About": "Yaazhi — Personal AI System by Santhosh"},
)

# ── Session state defaults ─────────────────────────────────────────────────────
if "session_id" not in st.session_state:
    st.session_state["session_id"] = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "api_base" not in st.session_state:
    st.session_state["api_base"] = f"http://localhost:{settings.app_port}"
if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""


# ── Helper ─────────────────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    """Return auth headers for API requests."""
    h: dict[str, str] = {}
    if st.session_state["api_key"]:
        h["X-API-Key"] = st.session_state["api_key"]
    return h


def _api(path: str) -> str:
    """Build a full API URL."""
    return f"{st.session_state['api_base']}{path}"


def _get_health() -> dict:
    """Fetch /health from the API. Returns empty dict on failure."""
    try:
        with httpx.Client(timeout=5.0) as c:
            resp = c.get(_api("/health"), headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return {}


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🤖 Yaazhi OS")
    st.caption(f"Version {settings.app_version} · Session `{st.session_state['session_id'][:8]}`")

    st.divider()

    page = st.radio(
        "Navigate",
        ["💬 Chat", "🧠 Memory", "📁 Knowledge", "📊 System", "⚙️ Settings"],
        label_visibility="collapsed",
    )

    st.divider()
    st.markdown("**System Status**")
    health = _get_health()
    services = health.get("services", {})
    for svc, ok in services.items():
        dot = "🟢" if ok else "🔴"
        st.caption(f"{dot} {svc}")

    if not services:
        st.caption("🔴 API unreachable")

    st.divider()
    st.session_state["api_key"] = st.text_input(
        "API Key", value=st.session_state["api_key"], type="password", key="sidebar_api_key"
    )


# ── Page: Chat ─────────────────────────────────────────────────────────────────

if page == "💬 Chat":
    st.title("💬 Chat with Yaazhi")

    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and "meta" in msg:
                meta = msg["meta"]
                agents = ", ".join(meta.get("agents_called", [])) or "none"
                st.caption(
                    f"🤖 Agents: `{agents}` · ⏱ {meta.get('processing_time_ms', 0)}ms · "
                    f"🧠 {meta.get('memories_used', 0)} memories"
                )

    col1, col2 = st.columns([5, 1])
    with col2:
        voice_file = st.audio_input("🎙", key="voice_input_chat")

    if voice_file:
        with st.spinner("Transcribing and thinking..."):
            try:
                with httpx.Client(timeout=60.0) as c:
                    resp = c.post(
                        _api("/api/v1/voice/chat"),
                        files={"audio": ("audio.wav", voice_file.read(), "audio/wav")},
                        params={"session_id": st.session_state["session_id"]},
                        headers=_headers(),
                    )
                    if resp.status_code == 200:
                        st.audio(resp.content, format="audio/wav")
                    else:
                        st.error(f"Voice chat error: {resp.status_code}")
            except Exception as exc:
                st.error(f"Voice chat failed: {exc}")

    if prompt := st.chat_input("Ask Yaazhi anything..."):
        st.session_state["messages"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            placeholder = st.empty()
            placeholder.markdown("⏳ Thinking...")
            try:
                with httpx.Client(timeout=120.0) as c:
                    resp = c.post(
                        _api("/api/v1/chat"),
                        json={
                            "message": prompt,
                            "session_id": st.session_state["session_id"],
                        },
                        headers=_headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    response_text = data.get("response", "No response received.")
                    placeholder.markdown(response_text)
                    agents = ", ".join(data.get("agents_called", [])) or "none"
                    st.caption(
                        f"🤖 Agents: `{agents}` · ⏱ {data.get('processing_time_ms', 0)}ms · "
                        f"🧠 {data.get('memories_used', 0)} memories"
                    )
                    meta = {
                        "agents_called": data.get("agents_called", []),
                        "processing_time_ms": data.get("processing_time_ms", 0),
                        "memories_used": data.get("memories_used", 0),
                    }
            except httpx.HTTPStatusError as exc:
                response_text = f"❌ API error {exc.response.status_code}: {exc.response.text[:200]}"
                placeholder.error(response_text)
                meta = {}
            except Exception as exc:
                response_text = f"❌ Connection error: {exc}"
                placeholder.error(response_text)
                meta = {}

        st.session_state["messages"].append(
            {"role": "assistant", "content": response_text, "meta": meta}
        )


# ── Page: Memory ───────────────────────────────────────────────────────────────

elif page == "🧠 Memory":
    st.title("🧠 Memory Explorer")

    search_query = st.text_input("Search memories", placeholder="Enter a query...")
    top_k = st.slider("Results", 1, 20, 5, key="memory_top_k")
    if st.button("🔍 Search", key="memory_search_btn") and search_query:
        with st.spinner("Searching..."):
            try:
                with httpx.Client(timeout=30.0) as c:
                    resp = c.get(
                        _api("/api/v1/memory/search"),
                        params={"q": search_query, "top_k": top_k},
                        headers=_headers(),
                    )
                    resp.raise_for_status()
                    results = resp.json()
                for i, mem in enumerate(results, 1):
                    with st.expander(f"{i}. [{mem.get('source','?')}] score={mem.get('score',0):.3f}"):
                        st.markdown(mem.get("text", ""))
                        st.json(mem.get("metadata", {}))
            except Exception as exc:
                st.error(f"Search failed: {exc}")

    st.divider()
    st.subheader("📊 Stats")
    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.get(_api("/api/v1/memory/stats"), headers=_headers())
            resp.raise_for_status()
            stats = resp.json()
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Memories", stats.get("total_memories", 0))
        col2.metric("ChromaDB", stats.get("chromadb_status", "?"))
        col3.metric("pgvector", stats.get("pgvector_status", "?"))
    except Exception as exc:
        st.warning(f"Stats unavailable: {exc}")

    st.divider()
    st.subheader("➕ Add Memory")
    mem_text = st.text_area("Memory text", key="add_mem_text")
    mem_tags = st.text_input("Tags (comma-separated)", key="add_mem_tags")
    if st.button("Add Memory", key="add_mem_btn") and mem_text:
        with st.spinner("Saving..."):
            try:
                with httpx.Client(timeout=30.0) as c:
                    resp = c.post(
                        _api("/api/v1/memory/add"),
                        json={
                            "text": mem_text,
                            "tags": [t.strip() for t in mem_tags.split(",") if t.strip()],
                            "source": "manual",
                        },
                        headers=_headers(),
                    )
                    resp.raise_for_status()
                    result = resp.json()
                st.success(f"Memory saved! ID: {result.get('memory_id', '?')}")
            except Exception as exc:
                st.error(f"Failed to save memory: {exc}")


# ── Page: Knowledge ────────────────────────────────────────────────────────────

elif page == "📁 Knowledge":
    st.title("📁 Knowledge Base")

    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.get(_api("/api/v1/memory/stats"), headers=_headers())
            resp.raise_for_status()
            stats = resp.json()
        st.metric("Total Indexed Chunks", stats.get("total_memories", 0))
    except Exception as exc:
        st.warning(f"Knowledge base stats unavailable: {exc}")

    st.divider()
    st.subheader("📤 Upload Document")
    uploaded = st.file_uploader(
        "Upload PDF, DOCX, or PPTX",
        type=["pdf", "docx", "pptx"],
        key="knowledge_upload",
    )
    if uploaded:
        save_path = f"{settings.knowledge_base_dir}/{uploaded.name}"
        with open(save_path, "wb") as fh:
            fh.write(uploaded.read())
        st.info(f"Saved to `{save_path}`. Ingesting...")
        with st.spinner("Ingesting document..."):
            try:
                with httpx.Client(timeout=120.0) as c:
                    resp = c.post(
                        _api("/api/v1/memory/ingest"),
                        json={"file_path": save_path},
                        headers=_headers(),
                    )
                    resp.raise_for_status()
                    result = resp.json()
                if result.get("was_skipped"):
                    st.warning("Document was already ingested (duplicate).")
                elif result.get("error"):
                    st.error(f"Ingestion error: {result['error']}")
                else:
                    st.success(
                        f"✅ Ingested `{result['file_name']}` — {result['chunks_created']} chunks created."
                    )
            except Exception as exc:
                st.error(f"Ingest failed: {exc}")


# ── Page: System ───────────────────────────────────────────────────────────────

elif page == "📊 System":
    st.title("📊 System Status")

    if st.button("🔄 Refresh Now", key="sys_refresh"):
        st.rerun()

    health = _get_health()
    if health:
        col1, col2, col3 = st.columns(3)
        col1.metric("Status", health.get("status", "?").upper())
        col2.metric("Uptime", f"{health.get('uptime_seconds', 0):.0f}s")
        col3.metric("Version", health.get("version", "?"))

        st.subheader("Services")
        services = health.get("services", {})
        cols = st.columns(4)
        for i, (svc, ok) in enumerate(services.items()):
            cols[i % 4].metric(svc, "✅ Online" if ok else "❌ Offline")
    else:
        st.error("Cannot reach Yaazhi API")

    st.divider()
    st.subheader("📈 Grafana Dashboard")
    grafana_url = "http://localhost:3000"
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(grafana_url)
            if r.status_code < 500:
                st.components.v1.iframe(grafana_url, height=600, scrolling=True)
    except Exception:
        st.info(f"Grafana is not accessible at {grafana_url}")

    time.sleep(30)
    st.rerun()


# ── Page: Settings ─────────────────────────────────────────────────────────────

elif page == "⚙️ Settings":
    st.title("⚙️ Settings")

    st.subheader("Model Configuration")
    try:
        import yaml
        from pathlib import Path
        models_content = Path("config/models.yaml").read_text(encoding="utf-8")
        st.code(models_content, language="yaml")
    except Exception as exc:
        st.warning(f"Could not load models.yaml: {exc}")

    st.divider()
    st.subheader("User Preferences")
    pref_key = st.text_input("Preference Key", key="pref_key_input")
    pref_val = st.text_input("Preference Value", key="pref_val_input")
    if st.button("💾 Save Preference", key="pref_save_btn") and pref_key and pref_val:
        with st.spinner("Saving preference..."):
            try:
                with httpx.Client(timeout=30.0) as c:
                    resp = c.post(
                        _api("/api/v1/chat"),
                        json={
                            "message": f"__set_preference {pref_key}={pref_val}",
                            "session_id": st.session_state["session_id"],
                        },
                        headers=_headers(),
                    )
                st.success("Preference command sent to Yaazhi.")
            except Exception as exc:
                st.error(f"Failed: {exc}")

    st.divider()
    st.subheader("Session Management")
    if st.button("🗑️ Clear Current Session", key="clear_session_btn"):
        with st.spinner("Clearing..."):
            try:
                with httpx.Client(timeout=10.0) as c:
                    c.delete(
                        _api(f"/api/v1/sessions/{st.session_state['session_id']}"),
                        headers=_headers(),
                    )
                st.session_state["messages"] = []
                st.session_state["session_id"] = str(uuid.uuid4())
                st.success("Session cleared. New session started.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    if st.button("🗜️ Consolidate Memory", key="consolidate_btn"):
        with st.spinner("Consolidating..."):
            try:
                with httpx.Client(timeout=60.0) as c:
                    resp = c.post(_api("/api/v1/memory/consolidate"), headers=_headers())
                    resp.raise_for_status()
                    result = resp.json()
                st.success(f"Compressed {result.get('sessions_compressed', 0)} sessions.")
            except Exception as exc:
                st.error(f"Consolidation failed: {exc}")
