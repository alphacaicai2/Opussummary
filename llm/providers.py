"""
LLM 提供商预设配置。

本模块定义了 OpusBrief 支持的所有 LLM 提供商配置，
包括 OpenAI、Anthropic、智谱、DeepSeek 等。

Example:
    >>> from llm.providers import get_provider, resolve_base_url
    >>> config = get_provider("openai")
    >>> print(config.name)  # "OpenAI"
    >>> base_url = resolve_base_url("openai")
    >>> print(base_url)  # "https://api.openai.com/v1"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """
    单个 LLM 提供商的配置信息。

    Attributes:
        id: 提供商唯一标识符（如 "openai", "anthropic"）
        name: 提供商显示名称（用于 UI 展示）
        base_url: API 基础地址
        default_model: 默认使用的模型名称
        openai_compatible: 是否兼容 OpenAI API 格式
        description: 提供商简短描述（可选）

    Example:
        >>> config = ProviderConfig(
        ...     id="openai",
        ...     name="OpenAI",
        ...     base_url="https://api.openai.com/v1",
        ...     default_model="gpt-4o-mini",
        ... )
        >>> config.id
        'openai'
    """

    id: str
    name: str
    base_url: str
    default_model: str
    openai_compatible: bool = True
    description: str = ""


# 预定义的 LLM 提供商配置表
PROVIDERS: Final[dict[str, ProviderConfig]] = {
    "openai": ProviderConfig(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        description="OpenAI 官方 API，支持 GPT-4o、GPT-4 等模型",
    ),
    "anthropic": ProviderConfig(
        id="anthropic",
        name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-opus-4-1-20250805",
        openai_compatible=True,
        description="Anthropic Claude 系列模型，已支持 OpenAI 兼容接口",
    ),
    "siliconflow": ProviderConfig(
        id="siliconflow",
        name="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        default_model="Qwen/Qwen2.5-7B-Instruct",
        description="国内 LLM 推理平台，支持多种开源模型",
    ),
    "zhipu_cn": ProviderConfig(
        id="zhipu_cn",
        name="智谱国内",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-plus",
        description="智谱 AI 国内站，GLM-4 系列模型",
    ),
    "zhipu_us": ProviderConfig(
        id="zhipu_us",
        name="智谱国际",
        base_url="https://open.bigmodel.us/api/paas/v4",
        default_model="glm-4-plus",
        description="智谱 AI 国际站，GLM-4 系列模型",
    ),
    "groq": ProviderConfig(
        id="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        description="Groq 高速推理平台，基于 LPU 架构",
    ),
    "deepseek": ProviderConfig(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        description="DeepSeek 深度求索，DeepSeek-V3 系列模型",
    ),
    "custom": ProviderConfig(
        id="custom",
        name="自定义",
        base_url="",
        default_model="",
        description="自定义 OpenAI 兼容 API 端点",
    ),
}


def get_provider(provider_id: str) -> ProviderConfig:
    """
    根据 ID 获取提供商配置。

    Args:
        provider_id: 提供商 ID（不区分大小写）

    Returns:
        ProviderConfig: 提供商配置对象

    Raises:
        KeyError: 当 provider_id 不在预定义列表中时

    Example:
        >>> config = get_provider("openai")
        >>> config.name
        'OpenAI'
        >>> config = get_provider("OPENAI")  # 不区分大小写
        >>> config.name
        'OpenAI'
    """
    key = provider_id.strip().lower()
    if key not in PROVIDERS:
        supported = ", ".join(sorted(PROVIDERS.keys()))
        raise KeyError(f"未知 provider: {provider_id!r}，可选: {supported}")
    return PROVIDERS[key]


def resolve_base_url(
    provider_id: str,
    custom_base_url: str | None = None,
) -> str:
    """
    解析最终使用的 API 基础地址。

    优先使用 custom_base_url（如果提供），否则使用提供商预设地址。
    对于 custom 提供商，必须提供 custom_base_url。

    Args:
        provider_id: 提供商 ID
        custom_base_url: 自定义 API 地址（可选）

    Returns:
        str: 最终的 API 基础地址（已去除尾部斜杠）

    Raises:
        ValueError: 当 provider 为 custom 但未提供 custom_base_url 时
        KeyError: 当 provider_id 无效时

    Example:
        >>> url = resolve_base_url("openai")
        >>> url
        'https://api.openai.com/v1'
        >>> url = resolve_base_url("custom", "https://my-api.example.com/v1")
        >>> url
        'https://my-api.example.com/v1'
    """
    # 优先使用自定义地址
    if custom_base_url and custom_base_url.strip():
        return custom_base_url.rstrip("/")

    # 使用提供商预设地址
    config = get_provider(provider_id)
    if config.base_url:
        return config.base_url.rstrip("/")

    # custom 提供商必须提供自定义地址
    raise ValueError(
        f"provider={provider_id!r} 时必须提供 custom_base_url 参数"
    )


def get_default_model(provider_id: str) -> str:
    """
    获取提供商的默认模型名称。

    Args:
        provider_id: 提供商 ID

    Returns:
        str: 默认模型名称

    Raises:
        KeyError: 当 provider_id 无效时
        ValueError: 当提供商没有预设默认模型时（如 custom）

    Example:
        >>> get_default_model("openai")
        'gpt-4o-mini'
    """
    config = get_provider(provider_id)
    if not config.default_model:
        raise ValueError(f"提供商 {provider_id!r} 没有预设默认模型，请手动指定")
    return config.default_model


def list_providers() -> list[ProviderConfig]:
    """
    获取所有支持的提供商配置列表。

    Returns:
        list[ProviderConfig]: 提供商配置列表（按 ID 排序）

    Example:
        >>> providers = list_providers()
        >>> len(providers) > 0
        True
        >>> providers[0].id
        'anthropic'
    """
    return [PROVIDERS[key] for key in sorted(PROVIDERS.keys())]
