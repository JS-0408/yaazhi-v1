"""
Yaazhi NotifierAgent — multi-channel notification delivery.

Sends alerts via WhatsApp (n8n webhook), email (n8n webhook), and
Telegram (direct Bot API). All sends are async with 3-attempt
exponential backoff retry.
"""

from __future__ import annotations

import asyncio
import time
from typing import Literal, Optional

import httpx
import logfire
import requests  # exported for tests that patch agents.notifier.requests

from config.settings import settings


class NotifierAgent:
    """
    Sends notifications across WhatsApp, email, and Telegram channels.

    All delivery attempts are made asynchronously with exponential
    backoff retry. Every send attempt is logged via Logfire regardless
    of outcome.

    Attributes:
        _http_client: Shared async httpx client.
        _max_retries: Number of retry attempts per send.
    """

    def __init__(self, webhook_url: Optional[str] = None) -> None:
        """Initialise the NotifierAgent with HTTP client configuration.

        Accepts an optional synchronous webhook_url for legacy notify() calls used in
        unit tests. The async send_* methods continue to use settings.n8n_webhook_base_url.
        """
        self._http_client: Optional[httpx.AsyncClient] = None
        self._max_retries: int = 3
        self.webhook_url = webhook_url or settings.n8n_webhook_base_url
        logfire.info("NotifierAgent initialised", channels=["whatsapp", "email", "telegram"])

    def __repr__(self) -> str:
        """Return string representation."""
        return f"NotifierAgent(max_retries={self._max_retries})"

    async def ping(self) -> bool:
        """
        Verify the n8n webhook endpoint is reachable.

        Returns:
            True if n8n base URL is reachable, False otherwise.
        """
        client = await self._get_client()
        try:
            base = settings.n8n_webhook_base_url.rstrip("/webhook").rstrip("/")
            response = await client.get(f"{base}/healthz", timeout=5.0)
            return response.status_code < 500
        except Exception as exc:
            logfire.warning("NotifierAgent ping failed", error=str(exc))
            return False

    async def _get_client(self) -> httpx.AsyncClient:
        """
        Get or create the shared async httpx client.

        Returns:
            Active httpx.AsyncClient instance.
        """
        if self._http_client is None or self._http_client.is_closed:
            auth = None
            if settings.n8n_basic_auth_user and settings.n8n_basic_auth_password:
                auth = (settings.n8n_basic_auth_user, settings.n8n_basic_auth_password)
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                auth=auth,
            )
        return self._http_client

    async def _post_with_retry(
        self, url: str, payload: dict, operation: str
    ) -> bool:
        """
        POST JSON to a URL with exponential backoff retry.

        Args:
            url: Target URL.
            payload: JSON payload dict.
            operation: Name of the operation for logging.

        Returns:
            True if any attempt succeeded (2xx response), False otherwise.
        """
        client = await self._get_client()

        for attempt in range(1, self._max_retries + 1):
            try:
                logfire.debug(f"Sending {operation}", attempt=attempt, url=url[:60])
                response = await client.post(url, json=payload, timeout=15.0)

                if response.status_code < 300:
                    logfire.info(
                        f"{operation} delivered",
                        status=response.status_code,
                        attempt=attempt,
                    )
                    return True

                logfire.warning(
                    f"{operation} got non-2xx response",
                    status=response.status_code,
                    attempt=attempt,
                )

            except httpx.TimeoutException as exc:
                logfire.warning(f"{operation} timeout", attempt=attempt, error=str(exc))
            except httpx.ConnectError as exc:
                logfire.warning(f"{operation} connection error", attempt=attempt, error=str(exc))
            except Exception as exc:
                logfire.error(f"{operation} unexpected error", attempt=attempt, error=str(exc))

            if attempt < self._max_retries:
                backoff = 2.0 ** attempt
                logfire.debug(f"{operation} retrying after {backoff}s")
                await asyncio.sleep(backoff)

        logfire.error(f"{operation} failed after {self._max_retries} attempts", url=url[:60])
        return False

    async def send_whatsapp(self, message: str) -> bool:
        """
        Send a WhatsApp message via the n8n webhook.

        Args:
            message: Message text to send.

        Returns:
            True if delivered successfully, False otherwise.
        """
        start = time.perf_counter()
        logfire.info("NotifierAgent.send_whatsapp", message_length=len(message))

        url = f"{settings.n8n_webhook_base_url}/yaazhi-notify"
        payload = {
            "message": message,
            "channel": "whatsapp",
            "priority": "normal",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        result = await self._post_with_retry(url, payload, "WhatsApp")
        logfire.info("send_whatsapp complete", success=result, duration_ms=int((time.perf_counter() - start) * 1000))
        return result

    async def send_email(self, subject: str, body: str, to: str) -> bool:
        """
        Send an email via the n8n webhook.

        Args:
            subject: Email subject line.
            body: Email body text or HTML.
            to: Recipient email address.

        Returns:
            True if delivered successfully, False otherwise.
        """
        start = time.perf_counter()
        logfire.info("NotifierAgent.send_email", subject=subject[:60], to=to)

        url = f"{settings.n8n_webhook_base_url}/yaazhi-email"
        payload = {
            "subject": subject,
            "body": body,
            "to": to,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        result = await self._post_with_retry(url, payload, "Email")
        logfire.info("send_email complete", success=result, duration_ms=int((time.perf_counter() - start) * 1000))
        return result

    async def send_telegram(self, message: str) -> bool:
        """
        Send a message directly via the Telegram Bot API.

        Args:
            message: Message text to send (supports Markdown).

        Returns:
            True if delivered successfully, False otherwise.
        """
        start = time.perf_counter()
        logfire.info("NotifierAgent.send_telegram", message_length=len(message))

        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logfire.warning("Telegram not configured, skipping send")
            return False

        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": settings.telegram_chat_id,
            "text": message[:4096],  # Telegram message limit
            "parse_mode": "Markdown",
        }
        client = await self._get_client()

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await client.post(url, json=payload, timeout=15.0)
                if response.status_code == 200:
                    logfire.info("Telegram message sent", attempt=attempt)
                    return True
                logfire.warning("Telegram non-200", status=response.status_code, attempt=attempt)
            except Exception as exc:
                logfire.warning("Telegram send error", attempt=attempt, error=str(exc))
            if attempt < self._max_retries:
                await asyncio.sleep(2.0 ** attempt)

        duration_ms = int((time.perf_counter() - start) * 1000)
        logfire.error("send_telegram failed", duration_ms=duration_ms)
        return False

    async def broadcast(
        self, message: str, channels: list[str] | None = None
    ) -> dict[str, bool]:
        """
        Send a message to multiple notification channels simultaneously.

        Args:
            message: Message text to broadcast.
            channels: List of channel names: 'whatsapp', 'email', 'telegram'.
                      Defaults to ['whatsapp'] if not provided.

        Returns:
            Dict mapping channel name to delivery success bool.
        """
        if channels is None:
            channels = ["whatsapp"]

        logfire.info("NotifierAgent.broadcast", channels=channels, message_length=len(message))

        send_tasks: dict[str, asyncio.Task[bool]] = {}
        if "whatsapp" in channels:
            send_tasks["whatsapp"] = asyncio.create_task(self.send_whatsapp(message))
        if "email" in channels:
            default_to = settings.owner_email
            send_tasks["email"] = asyncio.create_task(
                self.send_email("Yaazhi Notification", message, default_to)
            )
        if "telegram" in channels:
            send_tasks["telegram"] = asyncio.create_task(self.send_telegram(message))

        results: dict[str, bool] = {}
        for channel, task in send_tasks.items():
            try:
                results[channel] = await task
            except Exception as exc:
                logfire.error("Broadcast channel failed", channel=channel, error=str(exc))
                results[channel] = False

        logfire.info("Broadcast complete", results=results)
        return results

    async def task_complete_alert(
        self, task_name: str, result_summary: str, duration_seconds: float
    ) -> None:
        """
        Send a task completion notification with formatted content.

        Args:
            task_name: Name or description of the completed task.
            result_summary: Short summary of the task result.
            duration_seconds: How long the task took.
        """
        minutes = int(duration_seconds // 60)
        seconds = int(duration_seconds % 60)
        duration_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

        message = (
            f"✅ *Task Complete*\n\n"
            f"📋 Task: {task_name}\n"
            f"⏱ Duration: {duration_str}\n\n"
            f"📝 Summary:\n{result_summary[:500]}\n\n"
            f"— Yaazhi"
        )
        logfire.info("task_complete_alert", task=task_name[:60], duration=duration_str)
        await self.broadcast(message, channels=["whatsapp", "telegram"])

    def notify(self, title: str, body: str, channel: str = "whatsapp") -> requests.Response | None:
        """
        Synchronous legacy notification method used by some unit tests.

        Makes a blocking requests.post to the configured webhook_url. Returns
        the requests.Response or None on failure.
        """
        if not self.webhook_url:
            logfire.warning("NotifierAgent.notify called without webhook_url configured")
            return None
        payload = {"title": title, "body": body, "channel": channel}
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10.0)
            logfire.info("NotifierAgent.notify called", status=resp.status_code)
            return resp
        except Exception as exc:
            logfire.error("NotifierAgent.notify failed", error=str(exc))
            return None

    async def error_alert(
        self,
        error_type: str,
        details: str,
        severity: Literal["low", "medium", "high", "critical"] = "medium",
    ) -> None:
        """
        Send an error notification with severity-appropriate urgency.

        Args:
            error_type: Short error type identifier (e.g. 'DatabaseError').
            details: Detailed error description.
            severity: One of 'low', 'medium', 'high', 'critical'.
        """
        severity_icons = {
            "low": "ℹ️",
            "medium": "⚠️",
            "high": "🔴",
            "critical": "🚨🚨🚨",
        }
        icon = severity_icons.get(severity, "⚠️")
        channels = ["telegram"] if severity in ("low", "medium") else ["whatsapp", "telegram"]

        message = (
            f"{icon} *Yaazhi Error [{severity.upper()}]*\n\n"
            f"🔧 Type: {error_type}\n"
            f"📋 Details: {details[:400]}\n\n"
            f"— Yaazhi System Monitor"
        )
        logfire.error("error_alert sent", error_type=error_type, severity=severity)
        await self.broadcast(message, channels=channels)
