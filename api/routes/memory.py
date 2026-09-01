"""
Yaazhi Memory API Routes — path-traversal hardened.

Audit fixes applied (2026-05-10):
  SEC-7 / API-4 : Path whitelist validation in /ingest endpoint.
  API-5         : Query length limit on /search.
  New           : /ingest now also accepts UploadFile for direct upload.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import logfire
import redis.asyncio as aioredis
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from config.settings import settings
from memory.ingestion import IngestResult

router = APIRouter(tags=["memory"])


# ---------------------------------------------------------------------------
# Allowed ingest directories (SEC-7 fix)
# ---------------------------------------------------------------------------

def _get_allowed_dirs() -> list[Path]:
    """Return resolved allowed base directories for ingest. Built lazily."""
    dirs = []
    try:
        kb = Path(settings.knowledge_base_dir).resolve()
        dirs.append(kb)
    except Exception:
        pass
    # Also allow a dedicated uploads temp directory
    uploads = Path(tempfile.gettempdir()) / "yaazhi_uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    dirs.append(uploads.resolve())
    return dirs


def validate_ingest_path(raw_path: str) -> Path:
    """
    Resolve path and verify it is inside an allowed directory.

    SEC-7 FIX: Uses Path.relative_to() which handles symlink traversal
    correctly after resolve() follows all symlinks.

    Raises:
        HTTPException 403: If the resolved path is outside allowed dirs.
    """
    resolved = Path(raw_path).resolve()
    allowed_dirs = _get_allowed_dirs()
    for allowed in allowed_dirs:
        try:
            resolved.relative_to(allowed)
            return resolved
        except ValueError:
            continue
    raise HTTPException(
        status_code=403,
        detail=(
            f"Path '{raw_path}' is not within an allowed directory. "
            f"Allowed: {[str(d) for d in allowed_dirs]}"
        ),
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AddMemoryRequest(BaseModel):
    """Request body for manually adding a memory entry."""
    text: str = Field(..., min_length=1, max_length=8000)
    tags: list[str] = Field(default_factory=list)
    source: str = Field(default="manual")


class IngestPathRequest(BaseModel):
    """Request body for server-side path ingestion."""
    file_path: str = Field(..., min_length=1, max_length=512)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/search", summary="Semantic memory search")
async def search_memories(request: Request, q: str, top_k: int = 5) -> list[dict]:
    """
    Search the vector store for semantically similar memories.

    API-5 FIX: Query string limited to 500 characters.
    """
    # API-5: input length guard
    if len(q) > 500:
        raise HTTPException(status_code=422, detail="Query too long (max 500 chars).")

    logfire.debug("GET /memory/search", q=q[:80], top_k=top_k)
    state = request.app.state
    if not hasattr(state, "retriever"):
        raise HTTPException(status_code=503, detail="Retriever not initialised")

    top_k = max(1, min(top_k, 20))   # clamp between 1 and 20
    results = await state.retriever.retrieve(q, top_k=top_k)
    return [r.model_dump(mode="json") for r in results]


@router.get("/stats", summary="Vector store statistics")
async def memory_stats(request: Request) -> dict:
    """Return vector store statistics."""
    logfire.debug("GET /memory/stats")
    state = request.app.state
    vs = state.vector_store
    total = await vs.count()
    chroma_ok = vs._use_chroma
    pg_ok = vs._pg_pool is not None
    mem0_ok = vs._use_mem0 and vs._mem0 is not None

    return {
        "total_memories": total,
        "mem0_status": "active" if mem0_ok else "inactive",
        "chromadb_status": "active" if chroma_ok else "inactive",
        "pgvector_status": "active" if pg_ok else "inactive",
    }


@router.post("/add", summary="Manually add a memory entry")
async def add_memory(request: Request, body: AddMemoryRequest) -> dict:
    """Embed and store a manually provided text chunk."""
    logfire.debug("POST /memory/add", source=body.source, chars=len(body.text))
    state = request.app.state
    vs = state.vector_store
    metadata: dict = {"tags": body.tags, "manual": True}
    memory_id = await vs.add(
        body.text,
        metadata=metadata,
        source=body.source,
        user_id=settings.default_user_id,
    )
    logfire.info("Memory added manually", memory_id=memory_id)
    return {"memory_id": memory_id}


@router.delete("/{memory_id}", summary="Delete a memory entry by ID")
async def delete_memory(request: Request, memory_id: str) -> dict:
    """Delete a memory entry from the active vector store."""
    logfire.debug("DELETE /memory/{memory_id}", memory_id=memory_id)
    state = request.app.state
    deleted = await state.vector_store.delete(memory_id)
    return {"deleted": deleted}


@router.post("/ingest", summary="Ingest a document from server path")
async def ingest_document(request: Request, body: IngestPathRequest) -> dict:
    """
    Ingest a PDF, DOCX, or PPTX file from the server filesystem.

    SEC-7 FIX: Path is resolved and validated against an allowed-directory
    whitelist before any file operation.
    """
    logfire.debug("POST /memory/ingest (path)", file_path=body.file_path)
    state = request.app.state
    ingester = state.ingester

    # SEC-7: whitelist validation
    safe_path = validate_ingest_path(body.file_path)

    if not safe_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.file_path}")

    ext = safe_path.suffix.lower()
    dispatch = {
        ".pdf": ingester.ingest_pdf,
        ".docx": ingester.ingest_docx,
        ".pptx": ingester.ingest_pptx,
    }
    handler = dispatch.get(ext)
    if not handler:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported extension '{ext}'. Use .pdf, .docx, or .pptx.",
        )

    result: IngestResult = await handler(str(safe_path))
    logfire.info("Ingest complete (path)", file=body.file_path, chunks=result.chunks_created)
    return result.model_dump(mode="json")


@router.post("/upload", summary="Upload and ingest a document file")
async def upload_and_ingest(
    request: Request,
    file: UploadFile = File(...),
) -> dict:
    """
    Accept a file upload (PDF/DOCX/PPTX) and ingest it directly.

    File is saved to the yaazhi_uploads temp directory then ingested.
    """
    allowed_exts = {".pdf", ".docx", ".pptx"}
    filename = file.filename or "upload.pdf"
    ext = Path(filename).suffix.lower()

    if ext not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {sorted(allowed_exts)}",
        )

    uploads_dir = Path(tempfile.gettempdir()) / "yaazhi_uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    dest = uploads_dir / filename
    content = await file.read()

    # 50 MB size limit
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.")

    dest.write_bytes(content)
    logfire.info("POST /memory/upload: file saved", filename=filename, bytes=len(content))

    state = request.app.state
    ingester = state.ingester
    dispatch = {
        ".pdf": ingester.ingest_pdf,
        ".docx": ingester.ingest_docx,
        ".pptx": ingester.ingest_pptx,
    }
    result: IngestResult = await dispatch[ext](str(dest))
    logfire.info("POST /memory/upload: ingest complete", chunks=result.chunks_created)
    return result.model_dump(mode="json")


@router.get("/sessions", summary="List all conversation sessions with metadata")
async def list_all_sessions(request: Request) -> list[dict]:
    """Return all active sessions from Redis with message count and TTL info."""
    logfire.debug("GET /memory/sessions")
    state = request.app.state
    episodic = state.episodic
    await episodic._ensure_redis()
    r = episodic._redis   # reuse existing connection

    results: list[dict] = []
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match="session:*:messages", count=100)
        for key in keys:
            session_id = key.split(":")[1]
            count = await r.llen(key)
            ttl = await r.ttl(key)
            now_ts = int(datetime.now(timezone.utc).timestamp())
            last_active_ts = now_ts - (86400 - ttl) if ttl > 0 else now_ts
            results.append(
                {
                    "session_id": session_id,
                    "message_count": count,
                    "last_active": datetime.fromtimestamp(
                        last_active_ts, tz=timezone.utc
                    ).isoformat(),
                }
            )
        if cursor == 0:
            break
    return results


@router.post("/consolidate", summary="Summarise sessions with more than 50 messages")
async def consolidate_sessions(request: Request) -> dict:
    """Compress long sessions by generating and caching summaries."""
    logfire.debug("POST /memory/consolidate")
    state = request.app.state
    episodic = state.episodic
    await episodic._ensure_redis()
    r = episodic._redis

    compressed = 0
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match="session:*:messages", count=100)
        for key in keys:
            count = await r.llen(key)
            if count > 50:
                session_id = key.split(":")[1]
                try:
                    await episodic.summarize_session(session_id)
                    compressed += 1
                except Exception as exc:
                    logfire.warning(
                        "consolidate: summarize failed",
                        session_id=session_id[:8],
                        error=str(exc),
                    )
        if cursor == 0:
            break

    logfire.info("Memory consolidation complete", sessions_compressed=compressed)
    return {"sessions_compressed": compressed}
