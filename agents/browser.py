"""
Yaazhi Browser Agent — SSRF-hardened.

Audit fixes applied (2026-05-10):
  SEC-3 / A6 : validate_url() blocks all private IP ranges before navigation.
  A7          : close() is idempotent — safe to call multiple times.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import logfire

from config.settings import settings
from core.agent_registry import AgentRegistry

# ---------------------------------------------------------------------------
# SSRF URL validator
# ---------------------------------------------------------------------------

_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # Link-local / cloud metadata
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),     # Shared address space
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 private (ULA)
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]

_ALLOWED_SCHEMES: frozenset[str] = frozenset(["http", "https"])


def validate_url(url: str) -> tuple[bool, str]:
    """
    Validate a URL against SSRF blocklist.

    Checks:
      1. Scheme must be http or https.
      2. Every resolved IP of the hostname must not be in a private network.

    Returns:
        (True, "") if the URL is safe to navigate.
        (False, reason) if the URL is blocked.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"URL parse error: {exc}"

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, f"Scheme '{parsed.scheme}' not allowed. Only http/https permitted."

    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname."

    # Resolve all IPs for the hostname
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed for '{hostname}': {exc}"

    for family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        for blocked_net in _BLOCKED_NETWORKS:
            if ip_obj in blocked_net:
                logfire.warning(
                    "BrowserAgent: SSRF blocked",
                    url=url,
                    resolved_ip=ip_str,
                    blocked_network=str(blocked_net),
                )
                return False, (
                    f"SSRF blocked: '{hostname}' resolves to {ip_str} "
                    f"which is in private network {blocked_net}."
                )

    return True, ""


# ---------------------------------------------------------------------------
# BrowserAgent
# ---------------------------------------------------------------------------

@AgentRegistry.register("browser")
class BrowserAgent:
    """
    Headless browser automation using Playwright.

    All navigation targets are validated against the SSRF blocklist
    before any network request is made.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._closed: bool = False

    def __repr__(self) -> str:
        active = self._browser is not None and not self._closed
        return f"BrowserAgent(active={active})"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> None:
        """Lazy-initialise Playwright browser on first use."""
        if self._browser is not None and not self._closed:
            return
        from playwright.async_api import async_playwright  # type: ignore

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux aarch64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            accept_downloads=False,
        )
        self._page = await self._context.new_page()
        self._closed = False
        logfire.info("BrowserAgent: Playwright browser launched")

    async def ping(self) -> bool:
        """Check if Playwright is importable and browser can launch."""
        logfire.debug("BrowserAgent.ping called")
        try:
            await self._ensure_browser()
            logfire.info("BrowserAgent.ping success")
            return True
        except Exception as exc:
            logfire.error("BrowserAgent.ping failed", error=str(exc))
            return False

    async def close(self) -> None:
        """
        Close browser and Playwright gracefully.

        A7 FIX: Idempotent — safe to call multiple times.
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        logfire.info("BrowserAgent: closed")

    # ------------------------------------------------------------------
    # Navigation (SSRF validated)
    # ------------------------------------------------------------------

    async def navigate(self, url: str, wait_ms: int = 2000) -> str:
        """
        Navigate to a URL and return the page text content.

        SEC-3 FIX: URL is validated against private IP blocklist before navigation.

        Args:
            url    : Target URL (http/https only).
            wait_ms: Additional milliseconds to wait after page load.

        Returns:
            Plain-text page content (up to 50 000 chars).

        Raises:
            ValueError : If URL fails SSRF validation.
            RuntimeError: On navigation failure.
        """
        # SEC-3 / A6 FIX: validate before ANY network call
        valid, reason = validate_url(url)
        if not valid:
            logfire.error("BrowserAgent.navigate SSRF blocked", url=url, reason=reason)
            raise ValueError(f"URL blocked: {reason}")

        logfire.debug("BrowserAgent.navigate", url=url)
        t_start = time.time()
        await self._ensure_browser()

        try:
            response = await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_ms > 0:
                await self._page.wait_for_timeout(wait_ms)

            status = response.status if response else 0
            if status >= 400:
                logfire.warning("BrowserAgent.navigate HTTP error", url=url, status=status)

            content = await self._page.inner_text("body")
            content = content[:50_000]   # cap to 50k chars

            duration_ms = int((time.time() - t_start) * 1000)
            logfire.info(
                "BrowserAgent.navigate success",
                url=url,
                chars=len(content),
                status=status,
                duration_ms=duration_ms,
            )
            return content
        except Exception as exc:
            logfire.error("BrowserAgent.navigate failed", url=url, error=str(exc))
            raise RuntimeError(f"Navigation failed for {url}: {exc}") from exc

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def screenshot(self, url: str, output_path: str) -> str:
        """
        Navigate to a URL and save a full-page screenshot.

        URL is SSRF-validated before navigation.

        Args:
            url         : Target URL.
            output_path : Absolute path to save the PNG screenshot.

        Returns:
            The output_path string.
        """
        valid, reason = validate_url(url)
        if not valid:
            raise ValueError(f"URL blocked: {reason}")

        logfire.debug("BrowserAgent.screenshot", url=url, output=output_path)
        await self._ensure_browser()

        try:
            await self._page.goto(url, wait_until="networkidle", timeout=30000)
            await self._page.screenshot(path=output_path, full_page=True)
            logfire.info("BrowserAgent.screenshot success", url=url, path=output_path)
            return output_path
        except Exception as exc:
            logfire.error("BrowserAgent.screenshot failed", url=url, error=str(exc))
            raise RuntimeError(f"Screenshot failed for {url}: {exc}") from exc

    # ------------------------------------------------------------------
    # Structured extraction
    # ------------------------------------------------------------------

    async def extract(self, url: str, selector: str) -> list[str]:
        """
        Extract text from all elements matching a CSS selector.

        URL is SSRF-validated before navigation.

        Args:
            url      : Target URL.
            selector : CSS selector to match elements.

        Returns:
            List of text strings from matched elements.
        """
        valid, reason = validate_url(url)
        if not valid:
            raise ValueError(f"URL blocked: {reason}")

        logfire.debug("BrowserAgent.extract", url=url, selector=selector)
        await self._ensure_browser()

        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            elements = await self._page.query_selector_all(selector)
            texts: list[str] = []
            for el in elements:
                text = await el.inner_text()
                if text.strip():
                    texts.append(text.strip())
            logfire.info("BrowserAgent.extract success", url=url, elements=len(texts))
            return texts
        except Exception as exc:
            logfire.error("BrowserAgent.extract failed", url=url, error=str(exc))
            raise RuntimeError(f"Extract failed for {url}: {exc}") from exc

    # ------------------------------------------------------------------
    # High-level: browse_and_summarise
    # ------------------------------------------------------------------

    async def browse_and_summarise(
        self,
        url: str,
        task: str,
        session_id: str = "",
    ) -> str:
        """
        Navigate to a URL, extract text, then summarise using LLM.

        Args:
            url        : Target URL (SSRF-validated).
            task       : What information to extract from the page.
            session_id : Session UUID for logging.

        Returns:
            LLM-generated summary of the page relevant to the task.
        """
        page_text = await self.navigate(url)

        try:
            import litellm  # type: ignore

            model = settings.get_litellm_model("fast_tasks")
            prompt = (
                f"Task: {task}\n\n"
                f"Web page content from {url}:\n\n"
                f"{page_text[:8000]}\n\n"
                "Extract and summarise information relevant to the task. "
                "Be factual. Cite specific data points found on the page."
            )
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.1,
            )
            summary = response.choices[0].message.content or ""
            logfire.info("BrowserAgent.browse_and_summarise success", url=url, chars=len(summary))
            return summary.strip()
        except Exception as exc:
            logfire.error("BrowserAgent.browse_and_summarise LLM failed", error=str(exc))
            return page_text[:3000]   # fall back to raw text
