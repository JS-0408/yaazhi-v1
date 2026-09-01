"""
Yaazhi Folder Watcher and Knowledge Base Indexer.

Watches configured knowledge directories for new/modified files
and triggers automatic ingestion via DocumentIngester.

Usage:
    from memory.indexer import FolderWatcher
    watcher = FolderWatcher(ingester, notifier)
    await watcher.start()

CLI:
    python memory/indexer.py --scan
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import threading
from pathlib import Path
from typing import Callable, Optional

import logfire
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from agents.notifier import NotifierAgent
from config.settings import settings
from memory.ingestion import DocumentIngester, IngestResult


class _IngestionEventHandler(FileSystemEventHandler):
    """
    Watchdog event handler that queues file paths for ingestion.

    Implements 5-second debounce per file path to avoid processing
    the same file multiple times during rapid write sequences.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        debounce_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        self._queue = queue
        self._loop = loop
        self._debounce: dict[str, float] = {}
        self._debounce_secs = debounce_seconds

    def _should_process(self, path: str) -> bool:
        """Return True if enough time has passed since last event for this path."""
        now = time.monotonic()
        last = self._debounce.get(path, 0.0)
        if now - last >= self._debounce_secs:
            self._debounce[path] = now
            return True
        return False

    def on_created(self, event: FileSystemEvent) -> None:
        """Handle file creation events."""
        if not event.is_directory and self._should_process(event.src_path):
            asyncio.run_coroutine_threadsafe(
                self._queue.put(event.src_path), self._loop
            )

    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modification events."""
        if not event.is_directory and self._should_process(event.src_path):
            asyncio.run_coroutine_threadsafe(
                self._queue.put(event.src_path), self._loop
            )


class FolderWatcher:
    """
    Watches knowledge base directories for new documents and auto-ingests them.

    Monitored directories (created if absent):
      - {settings.knowledge_base_dir}/btech_notes
      - {settings.knowledge_base_dir}/ieee_papers
      - {settings.knowledge_base_dir}/projects

    Uses watchdog for cross-platform file system events with a 5-second
    debounce per file path to avoid duplicate processing.
    """

    _SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx"}

    def __init__(
        self,
        ingester: DocumentIngester,
        notifier: NotifierAgent,
    ) -> None:
        """
        Initialise the watcher with ingester and notifier dependencies.

        Args:
            ingester: DocumentIngester instance for processing files.
            notifier: NotifierAgent for broadcasting alerts.
        """
        self._ingester = ingester
        self._notifier = notifier
        self._observer = Observer()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._watch_dirs: list[str] = [
            os.path.join(settings.knowledge_base_dir, "btech_notes"),
            os.path.join(settings.knowledge_base_dir, "ieee_papers"),
            os.path.join(settings.knowledge_base_dir, "projects"),
        ]
        self._handler: Optional[_IngestionEventHandler] = None
        self._worker_task: Optional[asyncio.Task] = None

    def __repr__(self) -> str:
        """Return concise string representation."""
        alive = self._observer.is_alive() if hasattr(self._observer, "is_alive") else False
        return f"FolderWatcher(dirs={len(self._watch_dirs)}, observer_alive={alive})"

    async def ping(self) -> bool:
        """
        Check if the observer thread is alive (or has not been started yet).

        Returns:
            True if the observer is running or not yet started.
        """
        try:
            if hasattr(self._observer, "is_alive"):
                return self._observer.is_alive()
            return True
        except Exception as exc:
            logfire.error("FolderWatcher.ping failed", error=str(exc))
            return False

    async def start(self) -> None:
        """
        Create watch directories, schedule watchdog, and start the observer.

        Also starts an async worker coroutine that consumes the queue.
        """
        logfire.debug("FolderWatcher.start called")
        loop = asyncio.get_event_loop()

        for watch_dir in self._watch_dirs:
            Path(watch_dir).mkdir(parents=True, exist_ok=True)

        self._handler = _IngestionEventHandler(self._queue, loop)
        for watch_dir in self._watch_dirs:
            self._observer.schedule(self._handler, watch_dir, recursive=True)

        await asyncio.to_thread(self._observer.start)
        self._worker_task = asyncio.create_task(self._process_queue())
        logfire.info("FolderWatcher started", dirs=self._watch_dirs)

    async def stop(self) -> None:
        """Stop the watchdog observer and cancel the queue worker."""
        logfire.debug("FolderWatcher.stop called")
        if self._worker_task:
            self._worker_task.cancel()
        await asyncio.to_thread(self._observer.stop)
        await asyncio.to_thread(self._observer.join)
        logfire.info("FolderWatcher stopped")

    async def _process_queue(self) -> None:
        """
        Consume file paths from the queue and trigger ingestion.

        Runs as a persistent async task while the observer is active.
        """
        while True:
            file_path: str = await self._queue.get()
            ext = Path(file_path).suffix.lower()
            if ext not in self._SUPPORTED_EXTENSIONS:
                self._queue.task_done()
                continue
            logfire.info("FolderWatcher detected new file", path=file_path)
            try:
                dispatch = {
                    ".pdf": self._ingester.ingest_pdf,
                    ".docx": self._ingester.ingest_docx,
                    ".pptx": self._ingester.ingest_pptx,
                }
                result: IngestResult = await dispatch[ext](file_path)
                if not result.error and not result.was_skipped:
                    await self._notifier.broadcast(
                        f"📚 Ingested {result.file_name}: {result.chunks_created} chunks",
                        channels=["telegram"],
                    )
            except Exception as exc:
                logfire.error(
                    "FolderWatcher._process_queue error",
                    file=file_path,
                    error=str(exc),
                )
            finally:
                self._queue.task_done()

    async def scan_all(self) -> list[IngestResult]:
        """
        Run a full scan of all watch directories and ingest all found documents.

        Returns:
            Concatenated list of IngestResult from all watch directories.
        """
        logfire.debug("FolderWatcher.scan_all called")
        all_results: list[IngestResult] = []
        for watch_dir in self._watch_dirs:
            results = await self._ingester.ingest_folder(
                watch_dir, extensions=list(self._SUPPORTED_EXTENSIONS)
            )
            all_results.extend(results)
        logfire.info("FolderWatcher.scan_all complete", total=len(all_results))
        return all_results

    async def get_status(self) -> dict:
        """
        Return status dict showing file counts per directory and total memories.

        Returns:
            Dict with per-directory file counts and total VectorStore memory count.
        """
        status: dict = {}
        for watch_dir in self._watch_dirs:
            p = Path(watch_dir)
            if p.exists():
                count = sum(
                    1
                    for f in p.rglob("*")
                    if f.suffix.lower() in self._SUPPORTED_EXTENSIONS
                )
            else:
                count = 0
            status[watch_dir] = count

        total_memories = await self._ingester._vs.count()
        status["total_memories"] = total_memories
        return status


# ── CLI ────────────────────────────────────────────────────────────────────────

async def _cli_main() -> None:
    """Run the FolderWatcher CLI."""
    from rich.console import Console
    from rich.table import Table
    from memory.vector_store import VectorStore

    console = Console()
    parser = argparse.ArgumentParser(description="Yaazhi FolderWatcher CLI")
    parser.add_argument("--scan", action="store_true", help="Scan all watch dirs and ingest")
    args = parser.parse_args()

    vs = VectorStore()
    ingester = DocumentIngester(vs)
    notifier = NotifierAgent()
    watcher = FolderWatcher(ingester, notifier)

    if args.scan:
        console.print("[bold cyan]Scanning all knowledge directories...[/bold cyan]")
        results = await watcher.scan_all()

        table = Table(title="Ingestion Results")
        table.add_column("File")
        table.add_column("Chunks")
        table.add_column("Status")
        for r in results:
            status = "⏭ skipped" if r.was_skipped else ("❌ error" if r.error else "✅ ok")
            table.add_row(r.file_name, str(r.chunks_created), status)
        console.print(table)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(_cli_main())
