"""
OpusBrief - Discord Webhook Sender

Responsible for:
- Sending messages to Discord via webhook
- Supporting markdown and embed message formats
- Handling Discord's message length limits
- Automatic message chunking for long content
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import httpx

logger = logging.getLogger("opus.sender.discord")


# =============================================================================
# Constants
# =============================================================================

# Discord API limits
MAX_CONTENT_LENGTH = 2000
MAX_EMBED_TITLE_LENGTH = 256
MAX_EMBED_DESCRIPTION_LENGTH = 4096
MAX_EMBEDS_PER_MESSAGE = 10

# Rate limiting
RATE_LIMIT_RETRY_AFTER_HEADER = "Retry-After"
DEFAULT_TIMEOUT = 15.0
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0


class DiscordColor(IntEnum):
    """Common Discord embed colors."""

    DEFAULT = 0x2B90D9  # Blue
    SUCCESS = 0x2ECC71  # Green
    WARNING = 0xF39C12  # Orange
    ERROR = 0xE74C3C  # Red
    INFO = 0x3498DB  # Light Blue
    NEUTRAL = 0x95A5A6  # Gray


# =============================================================================
# Exceptions
# =============================================================================


class DiscordSenderError(Exception):
    """Base exception for Discord sender errors."""

    pass


class DiscordConnectionError(DiscordSenderError):
    """Raised when connection to Discord fails."""

    pass


class DiscordRateLimitError(DiscordSenderError):
    """Raised when rate limited by Discord."""

    def __init__(self, retry_after: float, message: str = "") -> None:
        self.retry_after = retry_after
        super().__init__(message or f"Rate limited. Retry after {retry_after} seconds")


class DiscordWebhookError(DiscordSenderError):
    """Raised when webhook execution fails."""

    def __init__(self, status_code: int, message: str = "") -> None:
        self.status_code = status_code
        super().__init__(message or f"Webhook failed with status {status_code}")


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class SendResult:
    """Result of a send operation."""

    success: bool
    message_id: int | None = None
    channel_id: int | None = None
    error: str | None = None


# =============================================================================
# Main Sender Class
# =============================================================================


class DiscordSender:
    """
    Discord Webhook sender with automatic chunking and rate limit handling.

    Features:
        - Send plain markdown messages
        - Send rich embed messages
        - Automatic message chunking for long content
        - Rate limit handling with retry
        - Connection testing

    Example:
        >>> with DiscordSender("https://discord.com/api/webhooks/...") as sender:
        ...     sender.send_markdown("Hello, World!")
        ...     sender.send_embed("Title", "Description", DiscordColor.SUCCESS)
        ...     sender.send_briefing(long_briefing_content)
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        client: httpx.Client | None = None,
        default_color: int = DiscordColor.DEFAULT,
    ) -> None:
        """
        Initialize the Discord sender.

        Args:
            webhook_url: The Discord webhook URL.
            timeout: Request timeout in seconds.
            max_retries: Maximum number of retries for rate limits (minimum 1).
            client: Optional pre-configured httpx.Client.
            default_color: Default color for embeds.

        Raises:
            ValueError: If webhook_url is empty or max_retries is invalid.
        """
        webhook_url = (webhook_url or "").strip()
        if not webhook_url:
            raise ValueError("webhook_url must not be empty")

        if not webhook_url.startswith(("https://discord.com/", "https://discordapp.com/")):
            logger.warning(
                "webhook_url does not appear to be a valid Discord webhook URL"
            )

        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")

        self.webhook_url = webhook_url
        self._timeout = timeout
        self._max_retries = max_retries
        self._default_color = default_color
        self._owns_client = client is None

        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                timeout=timeout,
                headers={
                    "User-Agent": "OpusBrief/1.0",
                    "Content-Type": "application/json",
                },
            )

    # =========================================================================
    # Context Manager Support
    # =========================================================================

    def __enter__(self) -> "DiscordSender":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client if we own it."""
        if self._owns_client:
            try:
                self._client.close()
            except Exception as e:
                logger.warning("Failed to close HTTP client: %s", e)

    # =========================================================================
    # Public API
    # =========================================================================

    def send_markdown(self, content: str) -> list[SendResult]:
        """
        Send a markdown-formatted message.

        If the content exceeds Discord's character limit, it will be
        automatically split into multiple messages.

        Args:
            content: The markdown content to send.

        Returns:
            List of SendResult for each message sent.
        """
        text = (content or "").strip()
        if not text:
            logger.debug("Empty content, nothing to send")
            return []

        chunks = self._chunk_text(text, MAX_CONTENT_LENGTH)
        results: list[SendResult] = []

        for i, chunk in enumerate(chunks, start=1):
            logger.debug("Sending markdown chunk %d/%d", i, len(chunks))

            try:
                response_data = self._post_with_retry(
                    {
                        "content": chunk,
                        "allowed_mentions": {"parse": []},
                    }
                )
                results.append(
                    SendResult(
                        success=True,
                        message_id=response_data.get("id"),
                        channel_id=response_data.get("channel_id"),
                    )
                )
            except DiscordSenderError as e:
                results.append(SendResult(success=False, error=str(e)))
                logger.error("Failed to send markdown chunk %d: %s", i, e)

        return results

    def send_embed(
        self,
        title: str,
        description: str,
        color: int = DiscordColor.DEFAULT,
        *,
        footer: str | None = None,
        url: str | None = None,
    ) -> list[SendResult]:
        """
        Send an embed message.

        If the description exceeds Discord's limit, it will be split
        into multiple embeds.

        Args:
            title: The embed title.
            description: The embed description.
            color: The embed color (hex integer).
            footer: Optional footer text.
            url: Optional URL for the title.

        Returns:
            List of SendResult for each message sent.
        """
        # Prepare title with length limit
        safe_title = (title or "Briefing").strip()[:MAX_EMBED_TITLE_LENGTH]
        safe_color = self._validate_color(color)

        # Chunk description
        description = description or ""
        chunks = self._chunk_text(description, MAX_EMBED_DESCRIPTION_LENGTH)

        if not chunks:
            chunks = ["(no content)"]

        results: list[SendResult] = []
        total_chunks = len(chunks)

        for i, chunk in enumerate(chunks, start=1):
            # Add page indicator if multiple chunks
            embed_title = safe_title
            if total_chunks > 1:
                embed_title = f"{safe_title} ({i}/{total_chunks})"[
                    :MAX_EMBED_TITLE_LENGTH
                ]

            embed: dict[str, Any] = {
                "title": embed_title,
                "description": chunk,
                "color": safe_color,
            }

            if url and i == 1:
                embed["url"] = url

            if footer and i == total_chunks:
                embed["footer"] = {"text": footer[:2048]}  # Footer text limit

            logger.debug("Sending embed chunk %d/%d", i, total_chunks)

            try:
                response_data = self._post_with_retry(
                    {
                        "embeds": [embed],
                        "allowed_mentions": {"parse": []},
                    }
                )
                results.append(
                    SendResult(
                        success=True,
                        message_id=response_data.get("id"),
                        channel_id=response_data.get("channel_id"),
                    )
                )
            except DiscordSenderError as e:
                results.append(SendResult(success=False, error=str(e)))
                logger.error("Failed to send embed chunk %d: %s", i, e)

        return results

    def send_briefing(self, briefing_content: str, title: str = "OpusBrief") -> int:
        """
        Send a briefing message.

        Automatically chooses between markdown and embed format based
        on content length.

        Args:
            briefing_content: The briefing content to send.
            title: Title for embed format (used if content is long).

        Returns:
            Number of messages successfully sent.
        """
        text = (briefing_content or "").strip()
        if not text:
            logger.debug("Empty briefing content, nothing to send")
            return 0

        # Use markdown for short content, embed for long content
        if len(text) <= MAX_CONTENT_LENGTH:
            results = self.send_markdown(text)
        else:
            results = self.send_embed(
                title=title,
                description=text,
                color=self._default_color,
                footer="Generated by OpusBrief",
            )

        success_count = sum(1 for r in results if r.success)
        logger.info("Sent briefing: %d/%d messages successful", success_count, len(results))

        return success_count

    def test_connection(self) -> tuple[bool, str]:
        """
        Test the webhook connection.

        Returns:
            Tuple of (success, message) indicating connection status.
        """
        try:
            response = self._client.get(self.webhook_url)

            if response.status_code == 200:
                data = response.json()
                webhook_name = data.get("name", "Unknown")
                guild_id = data.get("guild_id", "Unknown")
                channel_id = data.get("channel_id", "Unknown")

                message = (
                    f"Webhook '{webhook_name}' is valid. "
                    f"Guild: {guild_id}, Channel: {channel_id}"
                )
                logger.info("Discord webhook test successful: %s", message)
                return True, message

            if response.status_code == 404:
                return False, "Webhook not found. It may have been deleted."

            if response.status_code == 401:
                return False, "Unauthorized. Invalid webhook token."

            return False, f"Unexpected status code: {response.status_code}"

        except httpx.TimeoutException:
            error_msg = "Connection timed out"
            logger.error("Discord webhook test failed: %s", error_msg)
            return False, error_msg

        except httpx.ConnectError as e:
            error_msg = f"Connection failed: {e}"
            logger.error("Discord webhook test failed: %s", error_msg)
            return False, error_msg

        except Exception as e:
            error_msg = f"Unexpected error: {e}"
            logger.exception("Discord webhook test failed: %s", error_msg)
            return False, error_msg

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        POST to webhook with rate limit retry logic.

        Args:
            payload: The JSON payload to send.

        Returns:
            The response JSON data.

        Raises:
            DiscordRateLimitError: If rate limited after max retries.
            DiscordWebhookError: If the webhook returns an error status.
            DiscordConnectionError: If connection fails.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                return self._post(payload)
            except DiscordRateLimitError as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    delay = e.retry_after or (RETRY_BASE_DELAY * (2**attempt))
                    logger.warning(
                        "Rate limited, retrying in %.1f seconds (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        self._max_retries,
                    )
                    time.sleep(delay)
            except DiscordWebhookError:
                raise
            except DiscordConnectionError as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    delay = RETRY_BASE_DELAY * (2**attempt)
                    logger.warning(
                        "Connection error, retrying in %.1f seconds (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        self._max_retries,
                    )
                    time.sleep(delay)

        raise last_error or DiscordSenderError("Unknown error after retries")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a POST request to the webhook.

        Args:
            payload: The JSON payload to send.

        Returns:
            The response JSON data.

        Raises:
            DiscordRateLimitError: If rate limited.
            DiscordWebhookError: If the webhook returns an error status.
            DiscordConnectionError: If connection fails.
        """
        try:
            response = self._client.post(
                self.webhook_url,
                params={"wait": "true"},  # Wait for message to be sent
                json=payload,
            )

            # Handle rate limiting
            if response.status_code == 429:
                retry_after = float(
                    response.headers.get(RATE_LIMIT_RETRY_AFTER_HEADER, 5.0)
                )
                raise DiscordRateLimitError(retry_after)

            # Handle other errors
            if response.status_code >= 400:
                error_detail = ""
                try:
                    error_data = response.json()
                    error_detail = error_data.get("message", "")
                    for code_info in error_data.get("errors", {}).values():
                        if isinstance(code_info, dict):
                            error_detail += f" - {code_info.get('_errors', [{}])[0].get('message', '')}"
                except Exception:
                    pass

                raise DiscordWebhookError(
                    response.status_code,
                    f"Webhook error (status {response.status_code}): {error_detail}",
                )

            response.raise_for_status()

            if response.content:
                return response.json()
            return {}

        except httpx.TimeoutException as e:
            raise DiscordConnectionError(f"Request timed out: {e}") from e

        except httpx.ConnectError as e:
            raise DiscordConnectionError(f"Connection failed: {e}") from e

        except httpx.HTTPStatusError as e:
            raise DiscordWebhookError(e.response.status_code, str(e)) from e

    @staticmethod
    def _chunk_text(text: str, limit: int) -> list[str]:
        """
        Split text into chunks respecting Discord's character limits.

        Tries to split on newline boundaries first, then on word boundaries.

        Args:
            text: The text to split.
            limit: Maximum characters per chunk.

        Returns:
            List of text chunks.
        """
        # Normalize line endings
        text = text.replace("\r\n", "\n").strip()
        if not text:
            return []

        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        remaining = text

        while len(remaining) > limit:
            # Try to find a good split point
            split_pos = limit

            # First, try to split on double newline (paragraph)
            paragraph_break = remaining.rfind("\n\n", 0, limit + 1)
            if paragraph_break > limit // 3:
                split_pos = paragraph_break + 2
            else:
                # Try to split on single newline
                line_break = remaining.rfind("\n", 0, limit + 1)
                if line_break > limit // 3:
                    split_pos = line_break + 1
                else:
                    # Try to split on word boundary
                    word_break = remaining.rfind(" ", 0, limit + 1)
                    if word_break > limit // 3:
                        split_pos = word_break + 1

            chunk = remaining[:split_pos].rstrip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_pos:].lstrip()

        if remaining:
            chunks.append(remaining)

        return chunks

    @staticmethod
    def _validate_color(color: int | DiscordColor) -> int:
        """Validate and normalize a color value."""
        if isinstance(color, DiscordColor):
            return int(color)

        try:
            color_int = int(color)
            # Ensure color is in valid range
            if not (0 <= color_int <= 0xFFFFFF):
                logger.warning(
                    "Color value %s out of range, using default", color_int
                )
                return DiscordColor.DEFAULT
            return color_int
        except (ValueError, TypeError):
            return DiscordColor.DEFAULT
