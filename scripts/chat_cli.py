"""
Yaazhi Interactive Terminal Chat Client.

Connects to the running Yaazhi server (`http://127.0.0.1:8000`) and lets you
talk with Yaazhi interactively with full Supabase pgvector + Upstash Redis
session memory.

Usage:
    py scripts/chat_cli.py
"""

from __future__ import annotations

import sys
import uuid
import httpx


def main() -> None:
    base_url = "http://127.0.0.1:8000"
    session_id = f"santhosh-{uuid.uuid4().hex[:8]}"

    print("=" * 72)
    print(f" Yaazhi Interactive Terminal Chat | Session: {session_id}")
    print(f" Server: {base_url} | Type 'exit' or 'quit' to leave")
    print("=" * 72)

    with httpx.Client(base_url=base_url, timeout=90.0) as client:
        try:
            health = client.get("/health").json()
            svcs = [k for k, v in health.get("services", {}).items() if v]
            print(f"[Connected] Node: {health.get('node_id')} | Active services: {', '.join(svcs)}\n")
        except Exception as exc:
            print(f"[Error] Could not reach Yaazhi server at {base_url}: {exc}")
            print("Run `py scripts/run_local_8gb.py` first.")
            sys.exit(1)

        while True:
            try:
                user_msg = input("\nSanthosh > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not user_msg:
                continue
            if user_msg.lower() in {"exit", "quit", ":q"}:
                print("Goodbye!")
                break

            try:
                resp = client.post(
                    "/api/v1/chat",
                    json={
                        "message": user_msg,
                        "session_id": session_id,
                        "language": "auto",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                agents = ", ".join(data.get("agents_called", [])) or "direct"
                ms = data.get("processing_time_ms", 0)
                model = data.get("model_used", "")
                print(f"\nYaazhi [{agents} | {model} | {ms}ms] >\n{data.get('response', '')}")
            except Exception as exc:
                print(f"\n[Request Error] {exc}")


if __name__ == "__main__":
    main()
