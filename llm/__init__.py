"""
OpusBrief LLM 模块。

提供统一的 LLM 调用接口，支持多种 LLM 提供商。

主要组件:
- LLMClient: 统一 LLM 客户端
- PROVIDERS: 预定义的 LLM 提供商配置
- 异常类: LLMError 及其子类

LLMClient 实现了 briefing.generator.LLMClient Protocol，
可直接用于 BriefingGenerator。

Example:
    >>> from llm import LLMClient, get_provider, resolve_base_url
    >>>
    >>> # 获取提供商配置
    >>> config = get_provider("openai")
    >>> print(config.name)  # "OpenAI"
    >>>
    >>> # 创建客户端
    >>> client = LLMClient(
    ...     base_url=resolve_base_url("openai"),
    ...     api_key="sk-xxx",
    ...     model="gpt-4o-mini",
    ...     provider_id="openai",
    ... )
    >>>
    >>> # 同步调用
    >>> response = client.chat([
    ...     {"role": "user", "content": "Hello!"}
    ... ])
    >>> print(response)
    >>>
    >>> # 使用 complete 方法（BriefingGenerator Protocol）
    >>> response = client.complete(
    ...     system_prompt="You are a helpful assistant",
    ...     user_prompt="Hello!",
    ...     temperature=0.3,
    ...     max_tokens=1000,
    ... )
    >>>
    >>> # 测试连接
    >>> if client.test_connection():
    ...     print("连接成功")
    >>>
    >>> # 关闭客户端
    >>> client.close()

    >>> # 异步调用（推荐）
    >>> async with LLMClient(
    ...     base_url=resolve_base_url("anthropic"),
    ...     api_key="sk-ant-xxx",
    ...     model="claude-opus-4-1-20250805",
    ...     provider_id="anthropic",
    ...     api_style="anthropic",
    ... ) as client:
    ...     response = await client.chat_async([
    ...         {"role": "user", "content": "Hello!"}
    ...     ])
    ...     print(response)
"""

from .client import (
    ApiStyle,
    Article,
    ChatMessage,
    ChatRole,
    LLMAuthenticationError,
    LLMClient,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseFormatError,
    LLMServiceError,
)
from .providers import (
    PROVIDERS,
    ProviderConfig,
    get_default_model,
    get_provider,
    list_providers,
    resolve_base_url,
)

__all__ = [
    # 客户端
    "LLMClient",
    # 类型
    "ApiStyle",
    "Article",
    "ChatMessage",
    "ChatRole",
    # 提供商
    "PROVIDERS",
    "ProviderConfig",
    "get_provider",
    "get_default_model",
    "list_providers",
    "resolve_base_url",
    # 异常
    "LLMError",
    "LLMAuthenticationError",
    "LLMRateLimitError",
    "LLMRequestError",
    "LLMResponseFormatError",
    "LLMServiceError",
]

__version__ = "0.1.0"
