"""
Yaazhi ResearcherAgent — fast web research using Groq + DuckDuckGo.

Performs quick and deep research with Redis caching, rate limiting,
and automatic fallback from Groq to Ollama on API failure.

Audit fixes applied (2026-05-14):
  E-01 : Added @AgentRegistry.register("researcher") decorator.
  A-01 : Replaced synchronous Redis init with async lazy _ensure_redis().
  A-01 : Fixed cache key to use full 64-char SHA-256 digest (not [:16]).
  A-03 : Added SSRF validation to deep_fetch() via validate_url().
  B-02 : Replaced asyncio.to_thread(litellm.completion) with acompletion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Literal, Optional

import httpx
import logfire
import litellm
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

import redis.asyncio as aioredis

from config.settings import settings
from core.agent_registry import AgentRegistry
from core.state import DocumentResult


# ─── Rate Limiter ─────────────────────────────────────────────────────────────

class _RateLimiter:
    """Simple asyncio lock-based rate limiter."""

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self._max_calls = max_calls
        self._period = period_seconds
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire a rate limit slot, blocking if limit is exceeded."""
        async with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < self._period]
            if len(self._calls) >= self._max_calls:
                sleep_time = self._period - (now - self._calls[0])
                if sleep_time > 0:
                    logfire.debug("Rate limiter sleeping", seconds=round(sleep_time, 2))
                    await asyncio.sleep(sleep_time)
            self._calls.append(time.monotonic())


# ─── ResearcherAgent ──────────────────────────────────────────────────────────

@AgentRegistry.register("researcher")   # E-01 FIX: decorator was missing
class ResearcherAgent:
    """
    Performs web research using DuckDuckGo search and Groq LLM summarization.

    Provides two research depths:
    - 'quick': Top 5 search results, summarized with Groq (sub-second).
    - 'deep': Top 3 URLs fetched and parsed, summarized with Gemini.

    All search results are cached in Redis for 1 hour.

    A-01 FIX: Redis is initialised lazily via async _ensure_redis().
    A-03 FIX: deep_fetch() validates URLs against SSRF blocklist.
    B-02 FIX: All LLM calls use await litellm.acompletion() (true async).
    """

    def __init__(self) -> None:
        """Initialise the ResearcherAgent with model and cache configuration."""
        # E-02 VERIFIED SAFE: get_fallback_model exists in settings.py L310
        self._model: str = settings.get_litellm_model("fast_tasks")   # "research" → "fast_tasks"
        self._fallback_model: str = settings.get_fallback_model("fast_tasks")
        self._http_client: Optional[httpx.AsyncClient] = None
        self._rate_limiter = _RateLimiter(max_calls=10, period_seconds=60.0)
        self._redis: Optional[aioredis.Redis] = None   # A-01 FIX: lazy async init
        logfire.info("ResearcherAgent initialised", model=self._model)

    def __repr__(self) -> str:
        """Return string representation."""
        return f"ResearcherAgent(model={self._model!r})"

    # A-01 FIX: replaced blocking sync _init_redis() with async _ensure_redis()
    async def _ensure_redis(self) -> None:
        """Lazy-initialise async Redis connection on first use."""
        if self._redis is None:
            try:
                self._redis = aioredis.from_url(
                    settings.redis_url, encoding="utf-8", decode_responses=True
                )
                await self._redis.ping()
                logfire.debug("ResearcherAgent connected to Redis")
            except Exception as exc:
                logfire.warning("ResearcherAgent Redis unavailable, caching disabled", error=str(exc))
                self._redis = None

    async def ping(self) -> bool:
        """
        Verify the research model is reachable.

        Returns:
            True if model responds within 10 seconds, False otherwise.
        """
        try:
            # B-02 FIX: use acompletion (true async, no thread blocking)
            response = await litellm.acompletion(
                model=self._model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
                timeout=10,
            )
            return bool(response.choices[0].message.content)
        except Exception as exc:
            logfire.warning("ResearcherAgent ping failed", error=str(exc))
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

    def _cache_key(self, query: str) -> str:
        """
        Generate a Redis cache key for a search query.

        A-01 FIX: Uses full 64-char SHA-256 digest (previously [:16] caused
        collision risk of 1/2^64 instead of 1/2^128).

        Args:
            query: The search query string.

        Returns:
            Hex digest cache key string.
        """
        return f"yaazhi:research:{hashlib.sha256(query.encode()).hexdigest()}"

    async def _get_cached(self, key: str) -> Optional[str]:
        """
        Retrieve cached search result from Redis (async).

        A-01 FIX: Async method replaces previous sync version.
        """
        await self._ensure_redis()
        if self._redis is None:
            return None
        try:
            return await self._redis.get(key)
        except Exception as exc:
            logfire.debug("Cache get failed", error=str(exc))
            return None

    async def _set_cached(self, key: str, value: str, ttl: int = 3600) -> None:
        """
        Store a value in Redis cache with TTL (async).

        A-01 FIX: Async method replaces previous sync version.
        """
        await self._ensure_redis()
        if self._redis is None:
            return
        try:
            await self._redis.set(key, value, ex=ttl)
        except Exception as exc:
            logfire.debug("Cache set failed", error=str(exc))

    async def web_search(self, query: str) -> list[dict[str, str]]:
        """
        Search DuckDuckGo and return the top 5 results.

        Results are cached in Redis with TTL 3600 seconds.

        Args:
            query: Search query string.

        Returns:
            List of dicts with 'title', 'href', 'snippet' keys.

        Raises:
            RuntimeError: If DuckDuckGo search completely fails.
        """
        cache_key = self._cache_key(f"search:{query}")
        cached = await self._get_cached(cache_key)
        if cached:
            logfire.debug("Search cache hit", query=query[:40])
            return json.loads(cached)

        await self._rate_limiter.acquire()
        logfire.debug("DuckDuckGo search", query=query[:60])

        try:
            results: list[dict[str, str]] = await asyncio.to_thread(
                lambda: list(DDGS().text(keywords=query, max_results=5))
            )
            await self._set_cached(cache_key, json.dumps(results), ttl=3600)
            logfire.info("Web search complete", query=query[:40], result_count=len(results))
            return results
        except Exception as exc:
            logfire.error("DuckDuckGo search failed", query=query[:40], error=str(exc))
            raise RuntimeError(f"Web search failed: {exc}") from exc

    async def deep_fetch(self, url: str) -> str:
        """
        Fetch and extract clean text from a URL using httpx + BeautifulSoup4.

        A-03 FIX: URL is validated against SSRF blocklist before any network
        call. Prevents LLM-injected payloads from reaching cloud metadata
        endpoints (e.g. 169.254.169.254).

        Args:
            url: The URL to fetch and parse.

        Returns:
            Extracted text content (max 8000 characters).
        """
        # A-03 FIX: SSRF validation before ANY network call
        try:
            from agents.browser import validate_url
            safe, reason = validate_url(url)
            if not safe:
                logfire.warning(
                    "ResearcherAgent.deep_fetch SSRF blocked",
                    url=url[:80],
                    reason=reason,
                )
                return f"[URL blocked by SSRF filter: {reason}]"
        except ImportError:
            logfire.warning("ResearcherAgent: BrowserAgent validate_url not available, skipping SSRF check")

        cache_key = self._cache_key(f"fetch:{url}")
        cached = await self._get_cached(cache_key)
        if cached:
            logfire.debug("Fetch cache hit", url=url[:60])
            return cached

        client = await self._get_http_client()
        logfire.debug("Fetching URL", url=url[:80])

        try:
            response = await client.get(url, timeout=20.0)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            logfire.warning("URL fetch timeout", url=url[:60], error=str(exc))
            return f"[Fetch timeout for {url}]"
        except httpx.HTTPStatusError as exc:
            logfire.warning("URL fetch HTTP error", url=url[:60], status=exc.response.status_code)
            return f"[HTTP {exc.response.status_code} for {url}]"

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)
        import re
        text = re.sub(r"\s+", " ", text).strip()
        text = text[:8000]

        await self._set_cached(cache_key, text, ttl=3600)
        logfire.info("URL fetched", url=url[:60], text_length=len(text))
        return text

    async def _summarize_with_llm(self, content: str, topic: str, model: str) -> str:
        """
        Summarize content using the specified LiteLLM model.

        B-02 FIX: Uses await litellm.acompletion() — true async, no thread
        blocking. Previously used asyncio.to_thread(litellm.completion) which
        exhausted the ThreadPoolExecutor on ARM64 (4 cores, max 8 threads).

        Args:
            content: Text content to summarize.
            topic: The original research topic.
            model: LiteLLM model string to use.

        Returns:
            Summary string.
        """
        prompt = (
            f"Summarize the following content for someone researching: '{topic}'\n\n"
            f"Content:\n{content[:6000]}\n\n"
            f"Provide:\n"
            f"1. A 2-3 sentence summary\n"
            f"2. Key facts as bullet points\n"
            f"3. Confidence in accuracy (0-100%)\n"
        )
        # B-02 FIX: acompletion is fully async — zero thread usage
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.1,
            timeout=60,
        )
        return response.choices[0].message.content or "Summary unavailable"

    async def research(
        self, topic: str, depth: Literal["quick", "deep"] = "quick"
    ) -> DocumentResult:
        """
        Perform web research on a topic with configurable depth.

        quick mode: DuckDuckGo top 5 snippets → Groq summarization.
        deep mode: Top 3 URLs fetched and parsed → Gemini summarization.

        Args:
            topic: The research topic or question.
            depth: 'quick' for fast search, 'deep' for URL-level analysis.

        Returns:
            DocumentResult with summary, key_facts, sources, and confidence.
        """
        start_time = time.perf_counter()
        logfire.info("ResearcherAgent.research starting", topic=topic[:60], depth=depth)

        sources: list[str] = []
        raw_content: str = ""

        try:
            search_results = await self.web_search(topic)
            sources = [r.get("href", "") for r in search_results if r.get("href")]

            if depth == "quick":
                snippets = [
                    f"[{r.get('title', '')}]: {r.get('body', r.get('snippet', ''))}"
                    for r in search_results
                ]
                raw_content = "\n\n".join(snippets)

            elif depth == "deep":
                fetch_tasks = [self.deep_fetch(url) for url in sources[:3]]
                fetched_pages = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                page_texts = [
                    str(p) for p in fetched_pages if not isinstance(p, Exception) and isinstance(p, str)
                ]
                raw_content = "\n\n---\n\n".join(page_texts) if page_texts else ""
                if not raw_content:
                    raw_content = "\n\n".join([r.get("body", "") for r in search_results])

        except RuntimeError as exc:
            logfire.error("Research search phase failed", error=str(exc))
            return DocumentResult(
                summary=f"Research failed: {exc}",
                key_facts=[],
                sources=[],
                confidence_score=0.0,
            )

        summary: str = ""
        model_used: str = self._model
        for model, label in [(self._model, "primary"), (self._fallback_model, "fallback")]:
            try:
                summary = await self._summarize_with_llm(raw_content, topic, model)
                model_used = model
                break
            except Exception as exc:
                logfire.warning(f"Summarization failed on {label} model", model=model, error=str(exc))
                if label == "fallback":
                    summary = raw_content[:500] + "..." if len(raw_content) > 500 else raw_content

        import re
        key_facts = re.findall(r"[-•*]\s+(.+)", summary)
        if not key_facts:
            key_facts = [s.strip() for s in summary.split(". ") if len(s.strip()) > 20][:5]

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logfire.info(
            "ResearcherAgent.research complete",
            topic=topic[:40],
            depth=depth,
            sources=len(sources),
            model=model_used,
            duration_ms=duration_ms,
        )

        return DocumentResult(
            summary=summary,
            key_facts=key_facts[:8],
            sources=sources[:5],
            confidence_score=0.8 if sources else 0.4,
            raw_text=raw_content[:2000],
            page_count=0,
            word_count=len(summary.split()),
        )

    async def run(self, topic: str, depth: Literal["quick", "deep"] = "quick") -> str:
        """Run research and return a plain summary string (tests expect a str).

        Raises ValueError on empty topic.
        """
        if not topic or not topic.strip():
            raise ValueError("Empty research topic")
        doc = await self.research(topic, depth=depth)
        return doc.summary

    async def close(self) -> None:
        """Close all open connections gracefully."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
        if self._redis:
            await self._redis.aclose()
            self._redis = None
