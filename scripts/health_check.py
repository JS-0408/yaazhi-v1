"""
Yaazhi Health Checker — hardened CLI.

Audit fix INF-7: --no-llm flag skips live LLM completion (saves tokens).
New: --json flag outputs machine-readable JSON for monitoring tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import httpx
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, ".")
from config.settings import settings

console = Console()


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

async def check_ollama(client: httpx.AsyncClient) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        r = await client.get(f"{settings.ollama_base_url}/api/tags", timeout=10.0)
        models = [m["name"] for m in r.json().get("models", [])]
        return {"service": "Ollama", "status": "ok",
                "detail": f"{len(models)} models loaded", "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "Ollama", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


async def check_chromadb(client: httpx.AsyncClient) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        r = await client.get(f"{settings.chromadb_url}/api/v1/heartbeat", timeout=8.0)
        return {"service": "ChromaDB", "status": "ok", "detail": "heartbeat ok",
                "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "ChromaDB", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


async def check_postgres() -> dict[str, Any]:
    start = time.perf_counter()
    if not settings.postgres_url:
        return {"service": "PostgreSQL", "status": "skip", "detail": "POSTGRES_URL not set", "duration_ms": 0}
    try:
        import asyncpg
        conn = await asyncio.wait_for(asyncpg.connect(settings.postgres_url), timeout=8.0)
        version = await conn.fetchval("SELECT version()")
        has_vec = await conn.fetchval("SELECT COUNT(*) FROM pg_extension WHERE extname='vector'")
        await conn.close()
        pg_ver = str(version).split(" ")[1] if version else "?"
        return {"service": "PostgreSQL", "status": "ok" if has_vec else "warn",
                "detail": f"v{pg_ver}, pgvector={'✓' if has_vec else '✗'}",
                "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "PostgreSQL", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


async def check_redis() -> dict[str, Any]:
    start = time.perf_counter()
    try:
        import redis as rlib
        r = rlib.from_url(settings.redis_url, socket_timeout=5, decode_responses=True)
        r.ping()
        info = r.info("server")
        return {"service": "Redis", "status": "ok",
                "detail": f"v{info.get('redis_version','?')}, mem={info.get('used_memory_human','?')}",
                "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "Redis", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


async def check_fastapi(client: httpx.AsyncClient) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        r = await client.get(f"http://localhost:{settings.app_port}/health", timeout=10.0)
        data = r.json()
        return {"service": "Yaazhi API", "status": "ok" if data.get("status") == "ok" else "warn",
                "detail": f"status={data.get('status','?')}",
                "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "Yaazhi API", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


async def check_grafana(client: httpx.AsyncClient) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        r = await client.get("http://localhost:3000/api/health", timeout=8.0)
        data = r.json()
        return {"service": "Grafana", "status": "ok" if data.get("database") == "ok" else "warn",
                "detail": f"db={data.get('database','?')}",
                "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "Grafana", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


async def check_n8n(client: httpx.AsyncClient) -> dict[str, Any]:
    start = time.perf_counter()
    base = settings.n8n_webhook_base_url.rstrip("/webhook").rstrip("/")
    try:
        r = await client.get(f"{base}/healthz", timeout=8.0)
        return {"service": "n8n", "status": "ok" if r.status_code == 200 else "warn",
                "detail": f"HTTP {r.status_code}",
                "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "n8n", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


# INF-7 FIX: LLM check uses /health ping when --no-llm is set
async def check_groq_api(client: httpx.AsyncClient, no_llm: bool = False) -> dict[str, Any]:
    start = time.perf_counter()
    if not settings.groq_api_key:
        return {"service": "Groq API", "status": "skip", "detail": "GROQ_API_KEY not set", "duration_ms": 0}
    if no_llm:
        # INF-7 FIX: just verify the API key header is accepted, no completion call
        try:
            r = await client.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                timeout=10.0,
            )
            return {"service": "Groq API", "status": "ok" if r.status_code == 200 else "warn",
                    "detail": f"HTTP {r.status_code} (no-llm mode)",
                    "duration_ms": int((time.perf_counter()-start)*1000)}
        except Exception as exc:
            return {"service": "Groq API", "status": "fail", "detail": str(exc)[:60],
                    "duration_ms": int((time.perf_counter()-start)*1000)}
    try:
        import litellm
        resp = await asyncio.to_thread(
            litellm.completion,
            model="groq/llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "Say ok"}],
            max_tokens=5, timeout=15,
        )
        content = resp.choices[0].message.content or ""
        return {"service": "Groq API", "status": "ok", "detail": f"response='{content.strip()[:20]}'",
                "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "Groq API", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


async def check_gemini_api(no_llm: bool = False) -> dict[str, Any]:
    start = time.perf_counter()
    if not settings.google_api_key:
        return {"service": "Gemini API", "status": "skip", "detail": "GOOGLE_API_KEY not set", "duration_ms": 0}
    if no_llm:
        return {"service": "Gemini API", "status": "skip", "detail": "skipped (--no-llm)", "duration_ms": 0}
    try:
        import litellm
        resp = await asyncio.to_thread(
            litellm.completion,
            model="gemini/gemini-1.5-flash",
            messages=[{"role": "user", "content": "Say ok"}],
            max_tokens=5, timeout=15,
        )
        content = resp.choices[0].message.content or ""
        return {"service": "Gemini API", "status": "ok", "detail": f"response='{content.strip()[:20]}'",
                "duration_ms": int((time.perf_counter()-start)*1000)}
    except Exception as exc:
        return {"service": "Gemini API", "status": "fail", "detail": str(exc)[:60],
                "duration_ms": int((time.perf_counter()-start)*1000)}


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_table(results: list[dict[str, Any]]) -> None:
    table = Table(title="🤖 Yaazhi System Health", box=box.ROUNDED,
                  header_style="bold cyan", border_style="dim")
    table.add_column("Service", style="bold white", width=18)
    table.add_column("Status", width=12, justify="center")
    table.add_column("Detail", style="dim", width=48)
    table.add_column("Latency", width=10, justify="right")
    icons = {"ok": "[green]✅ OK[/green]", "fail": "[red]❌ FAIL[/red]",
             "warn": "[yellow]⚠ WARN[/yellow]", "skip": "[dim]⏭ SKIP[/dim]"}
    for r in results:
        table.add_row(r["service"], icons.get(r["status"], r["status"]),
                      r["detail"], f"{r['duration_ms']}ms" if r["duration_ms"] else "—")
    console.print()
    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(no_llm: bool = False, output_json: bool = False) -> int:
    if not output_json:
        console.print(Panel.fit(
            "[bold purple]Yaazhi Health Checker[/bold purple]\n[dim]Checking all system components...[/dim]",
            border_style="purple",
        ))

    async with httpx.AsyncClient(timeout=15.0) as client:
        checks = [
            check_ollama(client),
            check_chromadb(client),
            check_postgres(),
            check_redis(),
            check_n8n(client),
            check_fastapi(client),
            check_grafana(client),
            check_groq_api(client, no_llm=no_llm),   # INF-7 FIX
            check_gemini_api(no_llm=no_llm),
        ]
        raw = await asyncio.gather(*checks, return_exceptions=True)

    results: list[dict[str, Any]] = []
    for r in raw:
        if isinstance(r, Exception):
            results.append({"service": "Unknown", "status": "fail",
                            "detail": str(r)[:60], "duration_ms": 0})
        else:
            results.append(r)  # type: ignore

    if output_json:
        print(json.dumps(results, indent=2))
    else:
        render_table(results)

    ok = sum(1 for r in results if r["status"] == "ok")
    fail = sum(1 for r in results if r["status"] == "fail")
    skip = sum(1 for r in results if r["status"] == "skip")

    if not output_json:
        console.print(f"[bold]Summary:[/bold] {ok} OK · {fail} FAIL · {skip} SKIP\n")
        if fail == 0:
            console.print("[green bold]✅ All services healthy — Yaazhi is ready![/green bold]\n")
        elif fail < len(results):
            console.print(f"[yellow bold]⚠ {fail} service(s) failed — Yaazhi degraded[/yellow bold]\n")
        else:
            console.print("[red bold]❌ All services failed[/red bold]\n")

    return 0 if fail == 0 else (1 if fail < len(results) else 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Yaazhi system health checker")
    # INF-7 FIX: --no-llm skips live LLM completion requests
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM ping checks (saves tokens, use in routine monitoring)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON (for monitoring tools / CI)")
    args = parser.parse_args()
    code = asyncio.run(main(no_llm=args.no_llm, output_json=args.json))
    sys.exit(code)
