"""
Yaazhi ReaderAgent — document and URL reading with Gemini 1.5 Pro.

Uses Gemini's 2M token context window to read entire PDFs, web pages,
and documents. Supports summarization, key point extraction, and
document-grounded Q&A.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Literal, Optional

import httpx
import logfire
import litellm
from bs4 import BeautifulSoup

from config.settings import settings
from core.state import DocumentResult


class ReaderAgent:
    """
    Processes documents and URLs using Gemini 1.5 Pro's 2M token context.

    Handles PDFs (via pypdf2), web pages (via httpx + BeautifulSoup4),
    and raw text. All operations use Gemini 1.5 Pro as primary with
    Groq as fallback for shorter content.

    Attributes:
        _model: Primary LiteLLM model string (Gemini 1.5 Pro).
        _fallback_model: Fallback model string (Groq).
        _http_client: Shared async httpx client.
    """

    def __init__(self) -> None:
        """Initialise the ReaderAgent with model configuration."""
        self._model: str = settings.get_litellm_model("read_doc")
        self._fallback_model: str = settings.get_fallback_model("read_doc")
        self._http_client: Optional[httpx.AsyncClient] = None
        logfire.info("ReaderAgent initialised", model=self._model)

    def __repr__(self) -> str:
        """Return string representation."""
        return f"ReaderAgent(model={self._model!r})"

    async def ping(self) -> bool:
        """
        Verify the reading model is reachable.

        Returns:
            True if model responds, False otherwise.
        """
        try:
            response = await asyncio.to_thread(
                litellm.completion,
                model=self._model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
                timeout=15,
            )
            return bool(response.choices[0].message.content)
        except Exception as exc:
            logfire.warning("ReaderAgent ping failed", error=str(exc))
            return False

    async def _get_http_client(self) -> httpx.AsyncClient:
        """
        Get or create the shared async httpx client.

        Returns:
            Active httpx.AsyncClient instance.
        """
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
                follow_redirects=True,
            )
        return self._http_client

    def _validate_file_path(self, file_path: str) -> Path:
        """
        Validate that a file path is safe to read.

        Args:
            file_path: String path to validate.

        Returns:
            Resolved Path object.

        Raises:
            ValueError: If file doesn't exist or path is suspicious.
        """
        path = Path(file_path).resolve()
        if not path.exists():
            raise ValueError(f"File does not exist: {file_path}")
        if not path.is_file():
            raise ValueError(f"Path is not a file: {file_path}")
        # Prevent reading system files
        suspicious_prefixes = ["/etc", "/proc", "/sys", "/root", "/dev"]
        path_str = str(path)
        for prefix in suspicious_prefixes:
            if path_str.startswith(prefix):
                raise ValueError(f"Reading from {prefix} is not allowed")
        return path

    async def _call_llm(self, prompt: str, max_tokens: int = 2000) -> str:
        """
        Call the reading LLM with automatic fallback.

        Args:
            prompt: The prompt to send to the model.
            max_tokens: Maximum tokens in the response.

        Returns:
            LLM response content string.

        Raises:
            RuntimeError: If both primary and fallback models fail.
        """
        for model, label in [(self._model, "primary"), (self._fallback_model, "fallback")]:
            try:
                response = await asyncio.to_thread(
                    litellm.completion,
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=0.1,
                    timeout=120,
                )
                content = response.choices[0].message.content or ""
                logfire.debug("LLM call succeeded", model=model, response_length=len(content))
                return content
            except Exception as exc:
                logfire.warning("LLM call failed", model=model, label=label, error=str(exc))
                if label == "fallback":
                    raise RuntimeError(f"All reading models failed: {exc}") from exc

        raise RuntimeError("LLM call failed unexpectedly")

    async def read_pdf(self, file_path: str) -> DocumentResult:
        """
        Extract text from a PDF and analyse it with Gemini.

        Handles PDFs up to 500 pages by chunking text if needed.

        Args:
            file_path: Absolute or relative path to the PDF file.

        Returns:
            DocumentResult with summary, key facts, and page count.

        Raises:
            ValueError: If file path is invalid or not a PDF.
            RuntimeError: If text extraction or LLM call fails.
        """
        start_time = time.perf_counter()
        logfire.info("ReaderAgent.read_pdf called", file=file_path)

        try:
            safe_path = self._validate_file_path(file_path)
        except ValueError as exc:
            logfire.error("Invalid file path", path=file_path, error=str(exc))
            raise

        if safe_path.suffix.lower() != ".pdf":
            raise ValueError(f"File is not a PDF: {file_path}")

        try:
            import PyPDF2
        except ImportError as exc:
            raise RuntimeError("PyPDF2 not installed: pip install PyPDF2") from exc

        # Extract text from all pages
        try:
            all_text: list[str] = []
            page_count: int = 0

            with safe_path.open("rb") as fh:
                reader = PyPDF2.PdfReader(fh)
                page_count = len(reader.pages)
                for page_num, page in enumerate(reader.pages[:500]):  # Hard limit 500 pages
                    try:
                        page_text = page.extract_text() or ""
                        if page_text.strip():
                            all_text.append(f"[Page {page_num + 1}]\n{page_text}")
                    except Exception as exc:
                        logfire.warning("Page extraction failed", page=page_num + 1, error=str(exc))

        except PyPDF2.errors.PdfReadError as exc:
            raise RuntimeError(f"PDF is corrupted or encrypted: {exc}") from exc

        if not all_text:
            logfire.warning("PDF contains no extractable text", file=file_path)
            return DocumentResult(
                summary=f"PDF '{safe_path.name}' appears to be image-based with no extractable text. "
                        f"Consider using an OCR tool to extract content.",
                key_facts=["No extractable text found — PDF may be scanned"],
                sources=[str(safe_path)],
                confidence_score=0.1,
                page_count=page_count,
            )

        full_text = "\n\n".join(all_text)
        word_count = len(full_text.split())
        logfire.info("PDF text extracted", pages=page_count, words=word_count)

        # Chunk if very long
        max_chars = 800_000  # Stay within Gemini 2M token limit with safety margin
        if len(full_text) > max_chars:
            logfire.info("PDF too large, chunking", total_chars=len(full_text), max_chars=max_chars)
            full_text = full_text[:max_chars] + "\n\n[Content truncated — document is very large]"

        # Analyse with LLM
        prompt = (
            f"Analyse this PDF document and provide:\n\n"
            f"1. A comprehensive 3-5 sentence summary\n"
            f"2. Up to 10 key facts or findings as bullet points\n"
            f"3. The main topic or subject area\n\n"
            f"Document ('{safe_path.name}', {page_count} pages):\n\n{full_text[:600_000]}"
        )

        try:
            llm_output = await self._call_llm(prompt, max_tokens=2000)
        except RuntimeError as exc:
            logfire.error("PDF analysis LLM call failed", error=str(exc))
            llm_output = full_text[:500]

        import re
        key_facts = re.findall(r"[-•*\d\.]\s*(.+)", llm_output)[:10]

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logfire.info("ReaderAgent.read_pdf complete", pages=page_count, duration_ms=duration_ms)

        return DocumentResult(
            summary=llm_output,
            key_facts=key_facts,
            sources=[str(safe_path)],
            confidence_score=0.85,
            raw_text=full_text[:5000],
            page_count=page_count,
            word_count=word_count,
        )

    async def summarise_pdf(self, data, filename: str | None = None) -> str:
        """
        Summarise a PDF. Accepts either a file path (str) or bytes content with a filename.

        Returns a plain summary string (tests expect a simple string result).
        """
        # If a file path string is provided, delegate to read_pdf and return summary
        if isinstance(data, str):
            doc = await self.read_pdf(data)
            return doc.summary

        # If bytes provided (e.g., uploaded PDF content), try to extract text
        text = ""
        try:
            import io
            import PyPDF2

            reader = PyPDF2.PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            text = "\n\n".join(pages)
        except Exception:
            # Not a well-formed PDF or PyPDF2 missing — fallback to decoding bytes
            try:
                text = data.decode("utf-8", errors="ignore")
            except Exception:
                text = ""

        # Use the summarization interface (returns a string)
        summary = await self.summarize(text if text else "", style="simple")
        return summary

    async def answer_from_document(self, question: str, document_text: str) -> str:
        """Alias wrapper for answer_from_doc — tests expect this name."""
        return await self.answer_from_doc(document_text, question)  # type: ignore[arg-type]

    async def read_url(self, url: str) -> DocumentResult:
        """
        Fetch and analyse a web page using httpx + BeautifulSoup4 + Gemini.

        Args:
            url: The URL to fetch and analyse.

        Returns:
            DocumentResult with summary, key facts, and source URL.

        Raises:
            ValueError: If URL format is invalid.
        """
        start_time = time.perf_counter()
        logfire.info("ReaderAgent.read_url called", url=url[:80])

        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid URL (must start with http:// or https://): {url}")

        client = await self._get_http_client()
        try:
            response = await client.get(url, timeout=25.0)
            response.raise_for_status()
        except httpx.TimeoutException:
            logfire.warning("URL fetch timeout", url=url[:60])
            return DocumentResult(
                summary=f"Could not fetch {url}: request timed out",
                key_facts=["URL unreachable due to timeout"],
                sources=[url],
                confidence_score=0.0,
            )
        except httpx.HTTPStatusError as exc:
            logfire.warning("URL HTTP error", url=url[:60], status=exc.response.status_code)
            return DocumentResult(
                summary=f"Could not fetch {url}: HTTP {exc.response.status_code}",
                key_facts=[f"HTTP error {exc.response.status_code}"],
                sources=[url],
                confidence_score=0.0,
            )

        soup = BeautifulSoup(response.text, "html.parser")
        # Remove non-content elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
            tag.decompose()

        title = soup.find("title")
        page_title = title.get_text(strip=True) if title else url

        import re
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        word_count = len(text.split())

        logfire.info("URL content extracted", url=url[:60], words=word_count)

        prompt = (
            f"Analyse this web page and provide:\n"
            f"1. A 3-5 sentence summary of the main content\n"
            f"2. Up to 8 key facts or points as bullet points\n\n"
            f"Page: '{page_title}' ({url})\n\n{text[:50_000]}"
        )

        try:
            llm_output = await self._call_llm(prompt, max_tokens=1500)
        except RuntimeError as exc:
            logfire.error("URL analysis LLM failed", error=str(exc))
            llm_output = text[:500]

        key_facts = re.findall(r"[-•*]\s+(.+)", llm_output)[:8]

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logfire.info("ReaderAgent.read_url complete", url=url[:60], duration_ms=duration_ms)

        return DocumentResult(
            summary=llm_output,
            key_facts=key_facts,
            sources=[url],
            confidence_score=0.80,
            raw_text=text[:5000],
            page_count=0,
            word_count=word_count,
        )

    async def summarize(
        self, content: str, style: Literal["academic", "simple", "bullet"] = "academic"
    ) -> str:
        """
        Generate a styled summary of text content.

        Args:
            content: Text content to summarize.
            style: 'academic' (formal), 'simple' (plain English), 'bullet' (bullet list).

        Returns:
            Summary string in the requested style.
        """
        logfire.debug("ReaderAgent.summarize called", style=style, content_length=len(content))

        style_instructions = {
            "academic": "Write a formal academic-style summary with proper citations if sources are mentioned.",
            "simple": "Write a simple, plain-English summary that anyone can understand.",
            "bullet": "Write a bullet-point list of the main points, maximum 10 bullets.",
        }
        instruction = style_instructions.get(style, style_instructions["academic"])

        prompt = f"{instruction}\n\nContent:\n{content[:40_000]}\n\nSummary:"

        try:
            return await self._call_llm(prompt, max_tokens=1000)
        except RuntimeError as exc:
            logfire.error("Summarize failed", error=str(exc))
            return content[:300] + "..." if len(content) > 300 else content

    async def extract_key_points(self, content: str, topic: str) -> list[str]:
        """
        Extract topic-relevant key points from content.

        Args:
            content: Source text content.
            topic: The specific topic to focus on.

        Returns:
            List of key point strings (up to 10).
        """
        logfire.debug("ReaderAgent.extract_key_points called", topic=topic[:40])
        prompt = (
            f"From the following content, extract up to 10 key facts or points "
            f"specifically related to: '{topic}'\n\n"
            f"Format as a numbered list. Be precise and factual.\n\n"
            f"Content:\n{content[:30_000]}"
        )
        try:
            output = await self._call_llm(prompt, max_tokens=800)
        except RuntimeError as exc:
            logfire.error("Extract key points failed", error=str(exc))
            return [f"Error extracting points: {exc}"]

        import re
        points = re.findall(r"\d+\.\s+(.+)", output)
        if not points:
            points = re.findall(r"[-•*]\s+(.+)", output)
        return points[:10]

    async def answer_from_doc(self, content: str, question: str) -> str:
        """
        Answer a question using document content as the knowledge source.

        Implements document-grounded Q&A — the model is instructed to
        only use information present in the provided content.

        Args:
            content: Document text to use as the knowledge source.
            question: The question to answer.

        Returns:
            Answer string grounded in the document content.
        """
        logfire.debug("ReaderAgent.answer_from_doc called", question=question[:60])
        prompt = (
            f"Answer the following question using ONLY the information in the document below. "
            f"If the answer is not in the document, say 'This information is not found in the document.'\n\n"
            f"Question: {question}\n\n"
            f"Document:\n{content[:50_000]}\n\n"
            f"Answer:"
        )
        try:
            return await self._call_llm(prompt, max_tokens=1500)
        except RuntimeError as exc:
            logfire.error("Document Q&A failed", error=str(exc))
            return f"Unable to answer due to service error: {exc}"
