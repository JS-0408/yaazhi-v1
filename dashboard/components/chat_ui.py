"""
Yaazhi Chat UI Component.

Reusable Streamlit component for rendering a full conversation interface.
Can be embedded in any Streamlit page by calling render_chat().

Usage:
    from dashboard.components.chat_ui import render_chat
    render_chat(
        session_id="...",
        api_base_url="http://localhost:8000",
        api_key="...",
    )
"""

from __future__ import annotations

import httpx
import streamlit as st


def render_chat(
    session_id: str,
    api_base_url: str,
    api_key: str,
) -> None:
    """
    Render a complete conversational chat interface for a given session.

    Fetches conversation history from the API, displays messages in chat
    bubbles, shows metadata for assistant responses, supports voice input,
    and provides a text chat input. New messages trigger an API call and
    update the displayed conversation.

    Args:
        session_id: The conversation session UUID to display and interact with.
        api_base_url: Base URL of the Yaazhi API (e.g. 'http://localhost:8000').
        api_key: X-API-Key header value for authenticated requests.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    def _api(path: str) -> str:
        return f"{api_base_url}{path}"

    # ── Load history ─────────────────────────────────────────────────────────
    history: list[dict] = []
    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.get(
                _api(f"/api/v1/sessions/{session_id}/history"),
                headers=headers,
                params={"last_n": 50},
            )
            resp.raise_for_status()
            history = resp.json()
    except httpx.HTTPStatusError as exc:
        st.warning(f"Could not load history: HTTP {exc.response.status_code}")
    except Exception as exc:
        st.warning(f"History unavailable: {exc}")

    # ── Render messages ───────────────────────────────────────────────────────
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        with st.chat_message(role):
            if role == "assistant":
                st.markdown(content)
                agents = msg.get("agents_called", [])
                ptime = msg.get("processing_time_ms", 0)
                mems = msg.get("memories_used", 0)
                if agents or ptime:
                    st.caption(
                        f"🤖 Agents: `{', '.join(agents) or 'none'}` · "
                        f"⏱ {ptime}ms · 🧠 {mems} memories"
                    )
            else:
                st.markdown(content)
            st.code(content, language=None)

    # ── Voice input ───────────────────────────────────────────────────────────
    voice_file = st.audio_input("🎙 Speak to Yaazhi", key=f"voice_{session_id}")
    if voice_file:
        with st.spinner("Processing voice..."):
            placeholder = st.empty()
            try:
                with httpx.Client(timeout=90.0) as c:
                    resp = c.post(
                        _api("/api/v1/voice/chat"),
                        files={"audio": ("audio.wav", voice_file.read(), "audio/wav")},
                        params={"session_id": session_id},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    placeholder.success("Voice response ready")
                    st.audio(resp.content, format="audio/wav")
            except Exception as exc:
                placeholder.error(f"Voice chat failed: {exc}")

    # ── Text input ────────────────────────────────────────────────────────────
    if prompt := st.chat_input("Message Yaazhi...", key=f"chat_input_{session_id}"):
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            response_placeholder = st.empty()
            response_placeholder.markdown("⏳ Thinking...")
            try:
                with httpx.Client(timeout=120.0) as c:
                    resp = c.post(
                        _api("/api/v1/chat"),
                        json={"message": prompt, "session_id": session_id},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                response_text = data.get("response", "")
                response_placeholder.markdown(response_text)

                agents = data.get("agents_called", [])
                ptime = data.get("processing_time_ms", 0)
                mems = data.get("memories_used", 0)
                st.caption(
                    f"🤖 Agents: `{', '.join(agents) or 'none'}` · "
                    f"⏱ {ptime}ms · 🧠 {mems} memories"
                )
                st.code(response_text, language=None)
            except httpx.HTTPStatusError as exc:
                error_msg = f"API error {exc.response.status_code}: {exc.response.text[:200]}"
                response_placeholder.error(error_msg)
            except Exception as exc:
                response_placeholder.error(f"Request failed: {exc}")
