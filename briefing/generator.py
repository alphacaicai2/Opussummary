"""
OpusBrief Briefing Generator

This module provides the core briefing generation functionality:

- BriefingGenerator: Main class for generating briefings
- Support for multiple output formats (Markdown/HTML)
- Article validation and normalization
- Retry mechanism with structure validation
- Protocol-based LLM client interface

Example:
    >>> from briefing import BriefingGenerator, BriefingTemplate, OutputFormat, BriefingGenerateRequest
    >>>
    >>> # LLM client must implement the LLMClient protocol
    >>> generator = BriefingGenerator(llm_client=my_llm_client)
    >>>
    >>> # Using request object
    >>> request = BriefingGenerateRequest(
    ...     template=BriefingTemplate.GENERAL,
    ...     articles=articles_list,
    ...     timezone="Asia/Shanghai",
    ...     output_format=OutputFormat.BOTH,
    ... )
    >>> result = generator.generate(request)
    >>>
    >>> # Or using convenience method
    >>> result = generator.generate_simple(
    ...     template=BriefingTemplate.GENERAL,
    ...     articles=articles_list,
    ...     timezone="Asia/Shanghai",
    ...     output_format=OutputFormat.BOTH,
    ... )
    >>>
    >>> print(result.markdown)
    >>> print(result.html)
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from enum import StrEnum
from html import escape
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .templates import (
    MISSING_VALUE_TOKEN,
    REQUIRED_ARTICLE_FIELDS,
    BriefingTemplate,
    PromptTemplate,
    get_prompt_template,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# =============================================================================
# Logging
# =============================================================================

logger = logging.getLogger("opus.briefing.generator")


def _is_payload_too_large_error(exc: Exception) -> bool:
    """Detect provider-side request body too large errors (HTTP 413)."""
    message = str(exc).lower()
    markers = (
        "413",
        "payload too large",
        "request entity too large",
        "entity too large",
    )
    return any(marker in message for marker in markers)


# =============================================================================
# Exceptions
# =============================================================================

class BriefingGenerationError(RuntimeError):
    """
    Base exception for briefing generation failures.

    Attributes:
        message: Human-readable error description
        template_id: Template that failed (if available)
        attempt_count: Number of attempts made before failure
    """

    def __init__(
        self,
        message: str,
        *,
        template_id: str | None = None,
        attempt_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.template_id = template_id
        self.attempt_count = attempt_count


class OutputFormatError(BriefingGenerationError):
    """Raised when an invalid output format is specified."""

    def __init__(self, format_value: str) -> None:
        super().__init__(f"Invalid output format: '{format_value}'")
        self.format_value = format_value


class ArticleValidationError(BriefingGenerationError):
    """Raised when article validation fails."""

    def __init__(self, message: str, *, article_index: int | None = None) -> None:
        super().__init__(message)
        self.article_index = article_index


class LLMSyntaxError(BriefingGenerationError):
    """Raised when LLM output cannot be parsed or violates structure constraints."""
    pass


# =============================================================================
# Enums
# =============================================================================

class OutputFormat(StrEnum):
    """
    Available output formats for briefings.

    Attributes:
        MARKDOWN: Output only Markdown format
        HTML: Output only HTML format (rendered from Markdown)
        BOTH: Output both Markdown and HTML formats
    """
    MARKDOWN = "markdown"
    HTML = "html"
    BOTH = "both"


# =============================================================================
# Protocol Definitions
# =============================================================================

@runtime_checkable
class LLMClient(Protocol):
    """
    Protocol for LLM client implementations.

    Any client implementing this protocol can be used with BriefingGenerator.
    The client is responsible for handling API calls, retries, and errors.

    Example:
        >>> class MyLLMClient:
        ...     def complete(self, *, system_prompt, user_prompt, temperature, max_tokens):
        ...         # Call your LLM API here
        ...         return generated_text
    """

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """
        Generate completion from the LLM.

        Args:
            system_prompt: System instructions for the model
            user_prompt: User message with context and requirements
            temperature: Sampling temperature (0.0 to 1.0)
            max_tokens: Maximum tokens in the response

        Returns:
            Generated text (expected to be valid Markdown)

        Raises:
            Exception: If the LLM API call fails
        """
        ...


# =============================================================================
# Data Classes
# =============================================================================

@dataclass(slots=True)
class BriefingGenerateRequest:
    """
    Request object for briefing generation.

    Attributes:
        template: Template identifier (enum or string)
        articles: List of article dictionaries
        timezone: Target timezone for display (default: America/New_York)
        time_range_hours: Hours of content covered (default: 24)
        exchange_rate_context: Currency conversion instructions
        output_format: Desired output format(s)
        now_iso: Current timestamp (auto-generated if None)
        temperature: LLM temperature override
        max_tokens: LLM max tokens override
        article_content_max_length: Max characters per article content (default: 2500)
        prompt_char_budget: Maximum total characters for all articles in prompt (default: 40000)
        custom_system_prompt: Override system prompt (for custom/modified templates)
        custom_user_prompt_template: Override user prompt template (for custom/modified templates)
        custom_required_sections: Override required sections (for custom/modified templates)
    """
    template: BriefingTemplate | str
    articles: list[dict[str, Any]]
    timezone: str = "America/New_York"
    time_range_hours: int = 24
    exchange_rate_context: str = (
        "请使用当日汇率进行 USD 估算；若缺失汇率信息则 USD 估算写「未披露」。"
    )
    output_format: OutputFormat | str = OutputFormat.MARKDOWN
    now_iso: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    article_content_max_length: int = 2500
    prompt_char_budget: int = 40000
    # Custom template overrides (for database-stored customizations)
    custom_system_prompt: str | None = None
    custom_user_prompt_template: str | None = None
    custom_required_sections: tuple[str, ...] | None = None


@dataclass(slots=True)
class BriefingGenerateResult:
    """
    Result of briefing generation.

    Attributes:
        template: Template identifier used
        created_at: ISO timestamp of generation
        article_count: Number of articles processed
        markdown: Generated Markdown content (if requested)
        html: Generated HTML content (if requested)
        meta: Additional metadata about the generation
    """
    template: str
    created_at: str
    article_count: int
    markdown: str | None = None
    html: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def has_markdown(self) -> bool:
        """Check if Markdown output is available."""
        return self.markdown is not None

    @property
    def has_html(self) -> bool:
        """Check if HTML output is available."""
        return self.html is not None


# =============================================================================
# HTML Renderer (Abstracted)
# =============================================================================

class HTMLRenderer(ABC):
    """Abstract base class for HTML rendering strategies."""

    @abstractmethod
    def render(self, markdown_text: str) -> str:
        """Convert Markdown to HTML."""
        pass


class DefaultHTMLRenderer(HTMLRenderer):
    """
    Default HTML renderer using the markdown library.

    Falls back to escaped pre-formatted text if markdown library is unavailable.
    """

    def __init__(self) -> None:
        self._markdown_lib = self._try_import_markdown()

    @staticmethod
    def _try_import_markdown():  # type: ignore
        """Try to import the markdown library."""
        try:
            import markdown as md  # type: ignore
            return md
        except ImportError:
            logger.warning(
                "markdown library not installed. HTML output will be escaped plain text. "
                "Install with: pip install markdown"
            )
            return None

    def render(self, markdown_text: str) -> str:
        """
        Render Markdown to HTML.

        Args:
            markdown_text: Valid Markdown content

        Returns:
            HTML string
        """
        if self._markdown_lib is None:
            # Fallback: escape and wrap in pre tag
            return f'<pre class="briefing-plain">{escape(markdown_text)}</pre>'

        return self._markdown_lib.markdown(
            markdown_text,
            extensions=[
                "extra",        # Tables, code blocks, etc.
                "tables",       # Explicit table support
                "fenced_code",  # Fenced code blocks
                "sane_lists",   # Better list handling
                "toc",          # Table of contents (optional)
            ],
            output_format="html5",
        )


# =============================================================================
# Article Normalizer
# =============================================================================

class ArticleNormalizer:
    """
    Validates and normalizes article data for prompt generation.

    Ensures all required fields exist and truncates overly long content.
    Also enforces a total character budget across all articles.
    """

    def __init__(self, max_content_length: int = 2500, char_budget: int = 40000) -> None:
        self._max_content_length = max_content_length
        self._char_budget = char_budget
        self._ellipsis = "..."

    def normalize(self, articles: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
        """
        Normalize a list of articles.

        Args:
            articles: Raw article dictionaries

        Returns:
            List of normalized article dictionaries with string values

        Raises:
            ArticleValidationError: If articles list is empty
        """
        if not articles:
            raise ArticleValidationError("文章列表为空，无法生成简报")

        normalized: list[dict[str, str]] = []
        total_chars = 0

        for idx, article in enumerate(articles):
            if not isinstance(article, dict):
                logger.warning("跳过非法文章对象，index=%s", idx)
                continue

            item = self._normalize_article(article, idx)

            if item:
                # Check character budget before adding
                item_chars = len(item.get("content", ""))
                if total_chars + item_chars > self._char_budget:
                    # Try to fit with truncated content
                    remaining = self._char_budget - total_chars
                    if remaining > 0:
                        item["content"] = self._truncate_to_limit(item["content"], remaining)
                        normalized.append(item)
                        total_chars += len(item["content"])
                        logger.debug(
                            "Budget limit reached after %d articles, truncated last article",
                            len(normalized),
                        )
                    break

                normalized.append(item)
                total_chars += item_chars

        if not normalized:
            raise ArticleValidationError("所有文章均无效，无法生成简报")

        logger.info(
            "Normalized %d articles, total content chars: %d (budget: %d)",
            len(normalized),
            total_chars,
            self._char_budget,
        )

        return normalized

    def _normalize_article(
        self,
        article: dict[str, Any],
        index: int,
    ) -> dict[str, str] | None:
        """Normalize a single article."""
        item: dict[str, str] = {}

        for field_name in REQUIRED_ARTICLE_FIELDS:
            value = article.get(field_name)

            if value is None:
                item[field_name] = MISSING_VALUE_TOKEN
                continue

            # Convert to string and strip whitespace
            text = str(value).strip()
            item[field_name] = text if text else MISSING_VALUE_TOKEN

        # Truncate overly long content to save tokens
        if len(item["content"]) > self._max_content_length:
            original_length = len(item["content"])
            item["content"] = self._truncate_to_limit(item["content"], self._max_content_length)
            logger.debug(
                "截断文章内容，index=%s, original=%d, truncated=%d",
                index,
                original_length,
                len(item["content"]),
            )

        return item

    def _truncate_to_limit(self, text: str, limit: int) -> str:
        """
        Truncate text to a strict char limit while keeping ellipsis when possible.

        Args:
            text: Text to truncate
            limit: Maximum character limit

        Returns:
            Truncated text that fits within the limit
        """
        if limit <= 0:
            return ""
        if len(text) <= limit:
            return text
        if limit <= len(self._ellipsis):
            return text[:limit]
        return text[: limit - len(self._ellipsis)] + self._ellipsis


# =============================================================================
# Main Generator Class
# =============================================================================

class BriefingGenerator:
    """
    Main class for generating briefings from articles.

    This class orchestrates the briefing generation process:
    1. Validates and normalizes input articles
    2. Builds prompts using the selected template
    3. Calls the LLM client with retry logic
    4. Validates output structure
    5. Renders to requested formats

    Example:
        >>> generator = BriefingGenerator(llm_client=my_client)
        >>> request = BriefingGenerateRequest(
        ...     template=BriefingTemplate.GENERAL,
        ...     articles=[{"title": "...", "url": "...", ...}],
        ... )
        >>> result = generator.generate(request)
        >>>
        >>> # Or use the convenience method:
        >>> result = generator.generate_simple(
        ...     template="general",
        ...     articles=[{"title": "...", "url": "...", ...}],
        ... )
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        default_temperature: float = 0.2,
        default_max_tokens: int = 4096,
        max_retries: int = 2,
        html_renderer: HTMLRenderer | None = None,
    ) -> None:
        """
        Initialize the briefing generator.

        Args:
            llm_client: Client implementing the LLMClient protocol
            default_temperature: Default sampling temperature (0.0-1.0)
            default_max_tokens: Default max output tokens
            max_retries: Number of retries on structure validation failure
            html_renderer: Custom HTML renderer (default: markdown library)
        """
        self._llm_client = llm_client
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._max_retries = max_retries
        self._html_renderer = html_renderer or DefaultHTMLRenderer()

    def generate(self, request: BriefingGenerateRequest) -> BriefingGenerateResult:
        """
        Generate a briefing from the request.

        Args:
            request: Briefing generation request

        Returns:
            BriefingGenerateResult with markdown and/or html content

        Raises:
            BriefingGenerationError: If generation fails after retries
            ArticleValidationError: If article validation fails
            OutputFormatError: If output format is invalid
        """
        # Get base template and apply custom overrides if provided
        base_template = get_prompt_template(request.template)

        # Check if custom prompts are provided (from database customizations)
        if request.custom_system_prompt is not None or request.custom_user_prompt_template is not None:
            # Create a custom PromptTemplate with overrides
            template = PromptTemplate(
                template_id=base_template.template_id,
                name=base_template.name,
                description=base_template.description,
                system_prompt=request.custom_system_prompt or base_template.system_prompt,
                user_prompt_template=request.custom_user_prompt_template or base_template.user_prompt_template,
                required_sections=request.custom_required_sections if request.custom_required_sections is not None else base_template.required_sections,
            )
            logger.info(
                "Using custom template overrides for '%s'",
                request.template,
            )
        else:
            template = base_template

        output_format = self._parse_output_format(request.output_format)

        # Validate article_content_max_length
        if request.article_content_max_length < 1:
            raise ArticleValidationError(
                f"article_content_max_length must be >= 1, got {request.article_content_max_length}"
            )

        # Validate prompt_char_budget
        if request.prompt_char_budget < 1:
            raise ArticleValidationError(
                f"prompt_char_budget must be >= 1, got {request.prompt_char_budget}"
            )

        # Create normalizer with request-specific settings
        normalizer = ArticleNormalizer(
            max_content_length=request.article_content_max_length,
            char_budget=request.prompt_char_budget,
        )

        # Normalize articles
        normalized_articles = normalizer.normalize(request.articles)

        # Prepare context
        now_iso = request.now_iso or datetime.now(dt_timezone.utc).isoformat(timespec="seconds")

        # Build user prompt
        user_prompt = self._build_user_prompt(
            template=template,
            articles=normalized_articles,
            now_iso=now_iso,
            timezone_name=request.timezone,
            time_range_hours=request.time_range_hours,
            exchange_rate_context=request.exchange_rate_context,
        )

        # Generate markdown with retries
        markdown_text = self._generate_markdown_with_retry(
            template=template,
            user_prompt=user_prompt,
            temperature=request.temperature if request.temperature is not None else self._default_temperature,
            max_tokens=request.max_tokens if request.max_tokens is not None else self._default_max_tokens,
        )

        # Render HTML if requested
        html_text = None
        if output_format in {OutputFormat.HTML, OutputFormat.BOTH}:
            html_text = self._html_renderer.render(markdown_text)

        # Build result
        return BriefingGenerateResult(
            template=template.template_id.value,
            created_at=now_iso,
            article_count=len(normalized_articles),
            markdown=markdown_text if output_format in {OutputFormat.MARKDOWN, OutputFormat.BOTH} else None,
            html=html_text if output_format in {OutputFormat.HTML, OutputFormat.BOTH} else None,
            meta={
                "timezone": request.timezone,
                "time_range_hours": request.time_range_hours,
                "output_format": output_format.value,
                "template_name": template.name,
            },
        )

    def generate_simple(
        self,
        template: BriefingTemplate | str,
        articles: list[dict[str, Any]],
        **kwargs: Any,
    ) -> BriefingGenerateResult:
        """
        Convenience method for simple generation calls.

        Args:
            template: Template identifier
            articles: List of article dictionaries
            **kwargs: Additional arguments passed to BriefingGenerateRequest

        Returns:
            BriefingGenerateResult
        """
        request = BriefingGenerateRequest(template=template, articles=articles, **kwargs)
        return self.generate(request)

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _parse_output_format(self, output_format: OutputFormat | str) -> OutputFormat:
        """Parse and validate output format."""
        if isinstance(output_format, OutputFormat):
            return output_format

        try:
            return OutputFormat(output_format)
        except ValueError as exc:
            raise OutputFormatError(str(output_format)) from exc

    def _build_user_prompt(
        self,
        *,
        template: PromptTemplate,
        articles: list[dict[str, str]],
        now_iso: str,
        timezone_name: str,
        time_range_hours: int,
        exchange_rate_context: str,
    ) -> str:
        """Build the user prompt with all placeholders filled."""
        # Use compact JSON (no indent) to reduce token count
        articles_json = json.dumps(articles, ensure_ascii=False, separators=(",", ":"))
        return template.user_prompt_template.format(
            now_iso=now_iso,
            timezone=timezone_name,
            time_range_hours=time_range_hours,
            exchange_rate_context=exchange_rate_context or MISSING_VALUE_TOKEN,
            article_count=len(articles),
            articles_json=articles_json,
        )

    def _generate_markdown_with_retry(
        self,
        *,
        template: PromptTemplate,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Generate markdown with retry logic for API/empty-response failures."""
        current_prompt = user_prompt
        last_error = "未知错误"

        for attempt in range(self._max_retries + 1):
            try:
                output = self._llm_client.complete(
                    system_prompt=template.system_prompt,
                    user_prompt=current_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ).strip()

                if not output:
                    last_error = "模型返回空内容"
                    logger.warning(
                        "LLM 返回空内容，attempt=%s/%s",
                        attempt + 1,
                        self._max_retries + 1,
                    )
                    continue

                # Validate suggested sections using line-start regex for precision.
                # This is warn-only and does not block generation.
                missing_sections = [
                    section
                    for section in template.required_sections
                    if not re.search(rf"^{re.escape(section)}", output, re.MULTILINE)
                ]

                if missing_sections:
                    logger.warning(
                        "简报结构提示（仅提示，不影响结果）: missing=%s",
                        missing_sections,
                    )

                return output

            except Exception as exc:
                last_error = f"LLM API 调用失败: {exc}"
                logger.error(
                    "LLM API 调用异常，attempt=%s/%s, error=%s",
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                # Request-body-too-large errors are not retryable with identical input.
                if _is_payload_too_large_error(exc):
                    raise LLMSyntaxError(
                        f"简报生成失败（请求体过大）: {last_error}",
                        template_id=template.template_id.value,
                        attempt_count=attempt + 1,
                    ) from exc

        # All retries exhausted
        raise LLMSyntaxError(
            f"简报生成失败（重试耗尽）: {last_error}",
            template_id=template.template_id.value,
            attempt_count=self._max_retries + 1,
        )

    def _build_retry_prompt(
        self,
        original_prompt: str,
        missing_sections: list[str],
    ) -> str:
        """Build enhanced prompt for retry attempts."""
        return (
            f"{original_prompt}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "【结构纠错要求】\n\n"
            f"你上一次的输出遗漏了以下固定章节：\n{chr(10).join(f'- {s}' for s in missing_sections)}\n\n"
            "请严格按既定章节顺序重新输出完整的 Markdown 简报。\n"
            "注意：每个章节都必须存在，章节名必须精确匹配上述格式。"
        )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Main class
    "BriefingGenerator",
    # Request/Result
    "BriefingGenerateRequest",
    "BriefingGenerateResult",
    # Exceptions
    "BriefingGenerationError",
    "OutputFormatError",
    "ArticleValidationError",
    "LLMSyntaxError",
    # Enums
    "OutputFormat",
    # Protocols
    "LLMClient",
    # Renderers
    "HTMLRenderer",
    "DefaultHTMLRenderer",
    # Utilities
    "ArticleNormalizer",
]
