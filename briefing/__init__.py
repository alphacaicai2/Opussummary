"""
OpusBrief Briefing Module

This module provides intelligent briefing generation for Financial Advisors
focused on AI startup financing and secondary market transactions.

Key Components:
    - BriefingGenerator: Core class for generating briefings
    - Templates: Four specialized briefing templates
    - Output formats: Markdown and HTML support

Example:
    >>> from briefing import BriefingGenerator, BriefingTemplate, BriefingGenerateRequest
    >>> generator = BriefingGenerator(llm_client)
    >>> request = BriefingGenerateRequest(
    ...     template=BriefingTemplate.GENERAL,
    ...     articles=articles_list,
    ... )
    >>> result = generator.generate(request)
    >>> print(result.markdown)
    >>>
    >>> # Or use the convenience method:
    >>> result = generator.generate_simple(
    ...     template=BriefingTemplate.GENERAL,
    ...     articles=articles_list,
    ... )
"""

from .generator import (
    ArticleValidationError,
    BriefingGenerateRequest,
    BriefingGenerateResult,
    BriefingGenerationError,
    BriefingGenerator,
    DefaultHTMLRenderer,
    HTMLRenderer,
    LLMClient as LLMClientProtocol,
    LLMSyntaxError,
    OutputFormat,
    OutputFormatError,
)
from .templates import (
    MISSING_VALUE_TOKEN,
    REQUIRED_ARTICLE_FIELDS,
    BriefingTemplate,
    PromptTemplate,
    get_prompt_template,
    get_template_description,
    list_prompt_templates,
    validate_template_id,
)

__version__ = "1.0.0"

# Backward compatibility alias
LLMClient = LLMClientProtocol

__all__ = [
    # Generator exports
    "BriefingGenerator",
    "BriefingGenerationError",
    "BriefingGenerateRequest",
    "BriefingGenerateResult",
    "OutputFormat",
    "OutputFormatError",
    "ArticleValidationError",
    "LLMSyntaxError",
    "LLMClient",
    "LLMClientProtocol",
    "HTMLRenderer",
    "DefaultHTMLRenderer",
    # Template exports
    "BriefingTemplate",
    "PromptTemplate",
    "MISSING_VALUE_TOKEN",
    "REQUIRED_ARTICLE_FIELDS",
    "get_prompt_template",
    "get_template_description",
    "list_prompt_templates",
    "validate_template_id",
]
