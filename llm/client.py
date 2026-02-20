"""
统一 LLM 客户端。

本模块提供统一的 LLM 调用接口，支持 OpenAI 兼容 API 格式，
同时支持 Anthropic 原生 API。

核心特性:
- 异步核心 + 同步包装
- OpenAI 兼容 API 为主，Anthropic 原生可选
- 完整的错误类型体系
- 支持流式响应（OpenAI 兼容模式）
- 支持简报生成等业务场景

Example:
    >>> from llm import LLMClient
    >>> # 同步调用
    >>> client = LLMClient(
    ...     base_url="https://api.openai.com/v1",
    ...     api_key="sk-xxx",
    ...     model="gpt-4o-mini",
    ... )
    >>> response = client.chat([{"role": "user", "content": "Hello!"}])
    >>> print(response)
    >>> client.close()

    >>> # 异步调用（推荐）
    >>> async with LLMClient(...) as client:
    ...     response = await client.chat_async([{"role": "user", "content": "Hello!"}])
    ...     print(response)
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import TracebackType
from typing import Any, AsyncIterator, Literal, Mapping, Sequence, TypedDict

import httpx

# 配置模块日志
logger = logging.getLogger(__name__)

# 类型别名
ChatRole = Literal["system", "developer", "user", "assistant", "tool"]
ApiStyle = Literal["openai", "anthropic"]


# ============== 类型定义 ==============


class ChatMessage(TypedDict, total=False):
    """
    聊天消息结构。

    Attributes:
        role: 消息角色（system/developer/user/assistant/tool）
        content: 消息内容
        name: 可选的名称标识
    """

    role: ChatRole
    content: str
    name: str


class Article(TypedDict, total=False):
    """
    简报源文章结构。

    Attributes:
        id: 文章唯一标识
        title: 文章标题
        url: 文章链接
        source: 来源名称
        published_at: 发布时间
        summary: 文章摘要
        content: 文章正文
    """

    id: int | str
    title: str
    url: str
    source: str
    published_at: str
    summary: str
    content: str


# ============== 异常体系 ==============


class LLMError(Exception):
    """
    LLM 客户端基础异常类。

    所有 LLM 相关异常都继承此类，便于统一捕获。
    """

    def __init__(self, message: str, *, provider_id: str | None = None) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.message = message

    def __str__(self) -> str:
        if self.provider_id:
            return f"[{self.provider_id}] {self.message}"
        return self.message


class LLMRequestError(LLMError):
    """请求发送失败或网络错误。"""

    pass


class LLMAuthenticationError(LLMRequestError):
    """API Key 无效或鉴权失败。"""

    pass


class LLMRateLimitError(LLMRequestError):
    """触发 API 限流。"""

    pass


class LLMServiceError(LLMRequestError):
    """上游 LLM 服务异常（5xx 错误）。"""

    pass


class LLMResponseFormatError(LLMError):
    """响应格式不符合预期，解析失败。"""

    pass


# ============== 客户端实现 ==============


class LLMClient:
    """
    统一 LLM 客户端。

    支持 OpenAI 兼容 API 和 Anthropic 原生 API，
    提供同步和异步两种调用方式。

    Attributes:
        base_url: API 基础地址
        api_key: API Key（敏感信息，不应记录到日志）
        model: 模型名称
        provider_id: 提供商标识（用于日志和错误信息）
        api_style: API 风格（openai 或 anthropic）

    Example:
        >>> # 创建客户端
        >>> client = LLMClient(
        ...     base_url="https://api.openai.com/v1",
        ...     api_key="sk-xxx",
        ...     model="gpt-4o-mini",
        ...     provider_id="openai",
        ... )

        >>> # 同步调用
        >>> response = client.chat([
        ...     {"role": "user", "content": "Hello!"}
        ... ])
        >>> print(response)

        >>> # 测试连接
        >>> if client.test_connection():
        ...     print("连接成功")

        >>> # 关闭客户端
        >>> client.close()
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        provider_id: str = "openai",
        api_style: ApiStyle = "openai",
        timeout: float = 60.0,
    ) -> None:
        """
        初始化 LLM 客户端。

        Args:
            base_url: API 基础地址（如 https://api.openai.com/v1）
            api_key: API Key
            model: 模型名称
            provider_id: 提供商标识（默认 "openai"）
            api_style: API 风格，"openai" 或 "anthropic"（默认 "openai"）
            timeout: 请求超时时间，单位秒（默认 60.0）

        Raises:
            ValueError: 当必填参数为空时
        """
        # 参数校验
        if not base_url or not base_url.strip():
            raise ValueError("base_url 不能为空")
        if not api_key or not api_key.strip():
            raise ValueError("api_key 不能为空")
        if not model or not model.strip():
            raise ValueError("model 不能为空")

        # 保存配置
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider_id = provider_id
        self.api_style = api_style
        self._timeout = timeout

        # 创建 HTTP 客户端
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._build_headers(),
            timeout=httpx.Timeout(timeout),
            max_redirects=3,
        )

        logger.debug(
            "LLMClient 初始化完成: provider=%s, model=%s, base_url=%s",
            provider_id,
            model,
            self.base_url,
        )

    def _build_headers(self) -> dict[str, str]:
        """
        构建请求头。

        根据 api_style 使用不同的认证方式。
        """
        if self.api_style == "anthropic":
            return {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ============== 上下文管理 ==============

    async def __aenter__(self) -> "LLMClient":
        """异步上下文管理器入口。"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """异步上下文管理器退出，自动关闭连接。"""
        await self.close_async()

    # ============== 同步/异步转换 ==============

    def _run_sync(self, coro: Any) -> Any:
        """
        在同步上下文中运行异步协程。

        如果当前已在事件循环中，会抛出 RuntimeError，
        提示用户使用异步版本方法。

        Args:
            coro: 异步协程对象

        Returns:
            协程执行结果

        Raises:
            RuntimeError: 当已在事件循环中时
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 不在事件循环中，可以安全使用 asyncio.run
            return asyncio.run(coro)
        # 已在事件循环中，不能嵌套运行
        raise RuntimeError(
            "当前已在异步事件循环中，请使用 *_async 方法代替同步方法。"
            f" 例如：使用 chat_async() 代替 chat()"
        )

    # ============== 错误处理 ==============

    def _extract_error_message(self, response: httpx.Response) -> str:
        """
        从 HTTP 响应中提取错误信息。

        尝试解析 JSON 响应体，提取 message 或 error.message 字段。
        如果解析失败，返回原始文本。
        """
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            text = response.text or f"HTTP {response.status_code}"
            return text[:500]

        if isinstance(payload, dict):
            # OpenAI 风格错误: {"error": {"message": "..."}}
            err = payload.get("error")
            if isinstance(err, dict):
                if isinstance(err.get("message"), str):
                    return err["message"]
                if isinstance(err.get("type"), str):
                    return err["type"]
            # 通用错误: {"message": "..."}
            if isinstance(payload.get("message"), str):
                return payload["message"]

        # 无法解析，返回 JSON 片段
        return json.dumps(payload, ensure_ascii=False)[:500]

    def _raise_for_status(self, response: httpx.Response) -> None:
        """
        根据 HTTP 状态码抛出对应异常。

        Args:
            response: HTTP 响应对象

        Raises:
            LLMAuthenticationError: 401/403 错误
            LLMRateLimitError: 429 错误
            LLMServiceError: 5xx 错误
            LLMRequestError: 其他 4xx 错误
        """
        if response.status_code < 400:
            return

        message = self._extract_error_message(response)
        status = response.status_code

        if status in (401, 403):
            raise LLMAuthenticationError(
                f"鉴权失败: {message}",
                provider_id=self.provider_id,
            )
        if status == 429:
            raise LLMRateLimitError(
                f"API 限流: {message}",
                provider_id=self.provider_id,
            )
        if status >= 500:
            raise LLMServiceError(
                f"服务异常 ({status}): {message}",
                provider_id=self.provider_id,
            )
        raise LLMRequestError(
            f"请求失败 ({status}): {message}",
            provider_id=self.provider_id,
        )

    # ============== 核心对话方法 ==============

    async def chat_async(
        self,
        messages: Sequence[ChatMessage],
        temperature: float = 0.3,
        max_tokens: int = 1200,
        stream: bool = False,
    ) -> str:
        """
        异步对话方法。

        Args:
            messages: 消息列表
            temperature: 生成温度，0-2 之间（默认 0.3）
            max_tokens: 最大生成 token 数（默认 1200，必须 > 0）
            stream: 是否使用流式响应（默认 False）

        Returns:
            str: 模型生成的回复文本

        Raises:
            ValueError: 当参数不合法时
            LLMError: 各种 LLM 相关错误

        Example:
            >>> response = await client.chat_async([
            ...     {"role": "system", "content": "你是一个助手"},
            ...     {"role": "user", "content": "你好"},
            ... ])
        """
        # 参数校验
        if not messages:
            raise ValueError("messages 不能为空")
        if not 0 <= temperature <= 2:
            raise ValueError(f"temperature 必须在 0-2 之间，当前值: {temperature}")
        if max_tokens <= 0:
            raise ValueError(f"max_tokens 必须大于 0，当前值: {max_tokens}")
        if self.api_style == "anthropic":
            if stream:
                raise NotImplementedError(
                    "Anthropic 原生流式响应暂未实现，请使用 api_style='openai'"
                )
            return await self._chat_anthropic_native(messages, temperature, max_tokens)

        # OpenAI 兼容模式
        if stream:
            parts: list[str] = []
            async for chunk in self.chat_stream_async(messages, temperature, max_tokens):
                parts.append(chunk)
            return "".join(parts)

        return await self._chat_openai_compatible(messages, temperature, max_tokens)

    def chat(
        self,
        messages: Sequence[ChatMessage],
        temperature: float = 0.3,
        max_tokens: int = 1200,
        stream: bool = False,
    ) -> str:
        """
        同步对话方法。

        注意：如果在异步上下文中调用此方法，会抛出 RuntimeError。
        此时应使用 chat_async() 方法。

        Args:
            messages: 消息列表
            temperature: 生成温度，0-2 之间（默认 0.3）
            max_tokens: 最大生成 token 数（默认 1200，必须 > 0）
            stream: 是否使用流式响应（默认 False）

        Returns:
            str: 模型生成的回复文本

        Raises:
            ValueError: 当参数不合法时
            RuntimeError: 当在异步上下文中调用时
            LLMError: 各种 LLM 相关错误
        """
        return self._run_sync(
            self.chat_async(messages, temperature, max_tokens, stream)
        )

    async def chat_stream_async(
        self,
        messages: Sequence[ChatMessage],
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> AsyncIterator[str]:
        """
        异步流式对话方法。

        生成器模式返回，适用于需要实时显示响应的场景。

        Args:
            messages: 消息列表
            temperature: 生成温度，0-2 之间（默认 0.3）
            max_tokens: 最大生成 token 数（默认 1200，必须 > 0）

        Yields:
            str: 响应文本片段

        Raises:
            ValueError: 当参数不合法时
            LLMError: 各种 LLM 相关错误

        Example:
            >>> async for chunk in client.chat_stream_async(messages):
            ...     print(chunk, end="", flush=True)
        """
        # 参数校验
        if not messages:
            raise ValueError("messages 不能为空")
        if not 0 <= temperature <= 2:
            raise ValueError(f"temperature 必须在 0-2 之间，当前值: {temperature}")
        if max_tokens <= 0:
            raise ValueError(f"max_tokens 必须大于 0，当前值: {max_tokens}")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        logger.debug(
            "发起流式请求: model=%s, messages_count=%d",
            self.model,
            len(messages),
        )

        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=payload
            ) as resp:
                self._raise_for_status(resp)

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue

                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break

                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("流式响应 JSON 解析失败: %s", raw[:100])
                        continue

                    # 防御性处理空 choices
                    choices = chunk.get("choices", [])
                    if not choices or not isinstance(choices, list):
                        continue
                    delta = choices[0].get("delta", {})
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        yield text
        except httpx.HTTPError as exc:
            raise LLMRequestError(
                f"流式请求失败: {exc}",
                provider_id=self.provider_id,
            ) from exc

    # ============== OpenAI 兼容实现 ==============

    async def _chat_openai_compatible(
        self,
        messages: Sequence[ChatMessage],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """OpenAI 兼容 API 调用实现。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Calculate total prompt size for logging
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        system_chars = sum(
            len(str(m.get("content", "")))
            for m in messages
            if m.get("role") == "system"
        )
        user_chars = total_chars - system_chars

        logger.info(
            "=== LLM API Call ===\n"
            "  Provider: %s\n"
            "  Model: %s\n"
            "  Base URL: %s\n"
            "  Endpoint: POST %s/chat/completions\n"
            "  Messages: %d (system=%d chars, user=%d chars, total=%d chars)\n"
            "  Temperature: %.2f\n"
            "  Max Tokens: %d",
            self.provider_id,
            self.model,
            self.base_url,
            self.base_url,
            len(messages),
            system_chars,
            user_chars,
            total_chars,
            temperature,
            max_tokens,
        )

        try:
            resp = await self._client.post("/chat/completions", json=payload)
            logger.info(
                "=== LLM API Response ===\n"
                "  Status: %d\n"
                "  Model Used: %s",
                resp.status_code,
                self.model,
            )
        except httpx.HTTPError as exc:
            logger.error(
                "=== LLM API Network Error ===\n"
                "  Error: %s\n"
                "  Model: %s",
                exc,
                self.model,
            )
            raise LLMRequestError(
                f"网络请求失败: {exc}",
                provider_id=self.provider_id,
            ) from exc

        # Log error response details before raising
        if resp.status_code >= 400:
            logger.error(
                "=== LLM API Error Response ===\n"
                "  Status: %d\n"
                "  Model: %s\n"
                "  Response Body: %s",
                resp.status_code,
                self.model,
                resp.text[:1000] if resp.text else "(empty)",
            )

        self._raise_for_status(resp)

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMResponseFormatError(
                f"响应 JSON 解析失败: {resp.text[:200]}",
                provider_id=self.provider_id,
            ) from exc

        # 解析响应内容
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseFormatError(
                f"OpenAI 响应格式异常: {json.dumps(data, ensure_ascii=False)[:500]}",
                provider_id=self.provider_id,
            ) from exc

        # 处理不同类型的 content
        if isinstance(content, str):
            return content.strip()

        # 某些模型返回多模态内容块
        if isinstance(content, list):
            merged = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
            return merged.strip()

        raise LLMResponseFormatError(
            f"未知 content 类型: {type(content).__name__}",
            provider_id=self.provider_id,
        )

    # ============== Anthropic 原生实现 ==============

    def _split_messages_for_anthropic(
        self,
        messages: Sequence[ChatMessage],
    ) -> tuple[str | None, list[dict[str, str]]]:
        """
        将 OpenAI 格式消息转换为 Anthropic 格式。

        Anthropic 要求 system 消息单独传入，
        且只支持 user/assistant 两种对话角色。

        Returns:
            tuple: (system_prompt, converted_messages)

        Raises:
            LLMRequestError: 当没有有效的 user/assistant 消息时
        """
        system_parts: list[str] = []
        converted: list[dict[str, str]] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = str(msg.get("content", "")).strip()

            if not content:
                continue

            # system/developer 消息合并到 system prompt
            if role in ("system", "developer"):
                system_parts.append(content)
                continue

            # 只保留 user/assistant 消息
            if role not in ("user", "assistant"):
                continue

            converted.append({"role": role, "content": content})

        system = "\n\n".join(system_parts).strip() or None

        # 没有有效消息时抛出错误，而不是静默注入
        if not converted:
            raise LLMRequestError(
                "Anthropic API 需要至少一条 user 或 assistant 消息，"
                "当前消息列表中没有有效的对话消息",
                provider_id=self.provider_id,
            )

        return system, converted

    async def _chat_anthropic_native(
        self,
        messages: Sequence[ChatMessage],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Anthropic 原生 API 调用实现。"""
        system, converted_messages = self._split_messages_for_anthropic(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system

        logger.debug(
            "发起 Anthropic 原生请求: model=%s, messages_count=%d, has_system=%s",
            self.model,
            len(converted_messages),
            bool(system),
        )

        try:
            resp = await self._client.post("/messages", json=payload)
        except httpx.HTTPError as exc:
            raise LLMRequestError(
                f"网络请求失败: {exc}",
                provider_id=self.provider_id,
            ) from exc

        self._raise_for_status(resp)

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMResponseFormatError(
                f"响应 JSON 解析失败: {resp.text[:200]}",
                provider_id=self.provider_id,
            ) from exc

        # 解析 Anthropic 响应格式
        blocks = data.get("content", [])
        if not isinstance(blocks, list):
            raise LLMResponseFormatError(
                f"Anthropic content 格式异常: {json.dumps(data, ensure_ascii=False)[:500]}",
                provider_id=self.provider_id,
            )

        merged = "".join(
            item.get("text", "")
            for item in blocks
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()

        if not merged:
            raise LLMResponseFormatError(
                f"Anthropic 未返回文本内容: {json.dumps(data, ensure_ascii=False)[:500]}",
                provider_id=self.provider_id,
            )

        return merged

    # ============== BriefingGenerator Protocol 方法 ==============

    async def complete_async(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> str:
        """
        异步协议方法：为 BriefingGenerator 提供统一接口。

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            temperature: 生成温度 (0-2)
            max_tokens: 最大生成 token 数

        Returns:
            str: 模型生成的回复文本
        """
        messages: list[ChatMessage] = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": user_prompt})
        return await self.chat_async(messages, temperature=temperature, max_tokens=max_tokens)

    # ============== 业务方法 ==============

    def _format_articles_for_prompt(self, articles: Sequence[Article]) -> str:
        """
        将文章列表格式化为 Prompt 友好的文本。

        每篇文章截取最多 1200 字符，防止 token 溢出。
        """
        sections: list[str] = []

        for i, article in enumerate(articles, start=1):
            title = str(article.get("title", "（无标题）"))
            url = str(article.get("url", ""))
            source = str(article.get("source", ""))
            published_at = str(article.get("published_at", ""))

            # 优先使用 summary，其次使用 content
            raw_body = str(article.get("summary") or article.get("content") or "")
            # 压缩空白字符
            body = " ".join(raw_body.split())
            # 截断防止过长
            if len(body) > 1200:
                body = body[:1200] + "..."

            section = (
                f"[{i}] 标题: {title}\n"
                f"链接: {url}\n"
                f"来源: {source}\n"
                f"发布时间: {published_at}\n"
                f"正文/摘要: {body}"
            )
            sections.append(section)

        return "\n\n".join(sections)

    async def generate_briefing_async(
        self,
        articles: Sequence[Article],
        template_type: str,
        user_preferences: Mapping[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1800,
    ) -> str:
        """
        异步生成新闻简报。

        根据模板类型和用户偏好，基于文章列表生成结构化简报。

        Args:
            articles: 文章列表
            template_type: 模板类型（general/investment/ai_product/wechat_mp）
            user_preferences: 用户偏好设置（可选）
            temperature: 生成温度，0-2 之间（默认 0.2，更确定性）
            max_tokens: 最大生成 token 数（默认 1800，必须 > 0）

        Returns:
            str: Markdown 格式的简报内容

        Raises:
            ValueError: 当 articles 为空或参数不合法时
            LLMError: LLM 调用相关错误

        Example:
            >>> articles = [
            ...     {"title": "OpenAI 发布 GPT-5", "url": "...", "content": "..."},
            ... ]
            >>> briefing = await client.generate_briefing_async(
            ...     articles=articles,
            ...     template_type="ai_product",
            ... )
        """
        # 参数校验
        if not articles:
            raise ValueError("articles 不能为空")

        # 验证 template_type
        valid_templates = {"general", "investment", "ai_product", "wechat_mp"}
        template_type_clean = template_type.strip().lower() if template_type else ""
        if not template_type_clean:
            raise ValueError("template_type 不能为空")
        if template_type_clean not in valid_templates:
            raise ValueError(
                f"无效的 template_type: {template_type!r}，"
                f"可选: {', '.join(sorted(valid_templates))}"
            )

        if not 0 <= temperature <= 2:
            raise ValueError(f"temperature 必须在 0-2 之间，当前值: {temperature}")
        if max_tokens <= 0:
            raise ValueError(f"max_tokens 必须大于 0，当前值: {max_tokens}")

        preferences = dict(user_preferences or {})
        preferences_json = json.dumps(preferences, ensure_ascii=False)

        articles_text = self._format_articles_for_prompt(articles)

        messages: list[ChatMessage] = [
            {
                "role": "system",
                "content": (
                    "你是 OpusBrief 的新闻简报助手。"
                    "你的任务是根据用户提供的模板类型和文章内容，"
                    "生成结构化、可执行、面向金融顾问的 Markdown 简报。"
                    "\n\n"
                    "简报应包含：\n"
                    "1. 核心摘要（3-5 条要点）\n"
                    "2. 详细内容（按主题分组）\n"
                    "3. 行动建议（如有）"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"模板类型: {template_type}\n"
                    f"用户偏好: {preferences_json}\n\n"
                    f"请基于以下 {len(articles)} 篇文章生成简报：\n\n"
                    f"{articles_text}"
                ),
            },
        ]

        logger.info(
            "生成简报: template=%s, articles=%d, provider=%s",
            template_type,
            len(articles),
            self.provider_id,
        )

        return await self.chat_async(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def generate_briefing(
        self,
        articles: Sequence[Article],
        template_type: str,
        user_preferences: Mapping[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1800,
    ) -> str:
        """
        同步生成新闻简报。

        注意：如果在异步上下文中调用此方法，会抛出 RuntimeError。
        此时应使用 generate_briefing_async() 方法。
        """
        return self._run_sync(
            self.generate_briefing_async(
                articles,
                template_type,
                user_preferences,
                temperature,
                max_tokens,
            )
        )

    # ============== 连接测试 ==============

    async def test_connection_async(self) -> bool:
        """
        异步测试 API 连接是否正常。

        发送一个简单的 ping 消息，验证 API Key 和网络连接。

        Returns:
            bool: 连接成功返回 True，失败返回 False

        Example:
            >>> if await client.test_connection_async():
            ...     print("连接成功")
            ... else:
            ...     print("连接失败")
        """
        try:
            result = await self.chat_async(
                messages=[{"role": "user", "content": "Reply with: pong"}],
                temperature=0.0,
                max_tokens=8,
            )
            success = bool(result.strip())
            logger.info(
                "连接测试: provider=%s, success=%s",
                self.provider_id,
                success,
            )
            return success
        except LLMError as exc:
            logger.warning(
                "连接测试失败: provider=%s, error=%s",
                self.provider_id,
                str(exc),
            )
            return False

    def test_connection(self) -> bool:
        """
        同步测试 API 连接是否正常。

        注意：如果在异步上下文中调用此方法，会抛出 RuntimeError。
        此时应使用 test_connection_async() 方法。
        """
        return self._run_sync(self.test_connection_async())

    # ============== Protocol 适配器 ==============

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> str:
        """
        Protocol 适配方法，用于兼容 briefing.generator.LLMClient 协议。

        将 system_prompt 和 user_prompt 转换为 chat 消息格式调用底层 LLM。

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            temperature: 生成温度 (0-2)
            max_tokens: 最大生成 token 数

        Returns:
            str: 模型生成的回复文本
        """
        # Log prompt sizes for debugging
        sys_len = len(system_prompt) if system_prompt else 0
        user_len = len(user_prompt) if user_prompt else 0
        logger.info(
            "=== BriefingGenerator.complete() ===\n"
            "  Model: %s\n"
            "  System Prompt: %d chars\n"
            "  User Prompt: %d chars\n"
            "  Total Prompt: %d chars\n"
            "  Temperature: %.2f\n"
            "  Max Tokens: %d",
            self.model,
            sys_len,
            user_len,
            sys_len + user_len,
            temperature,
            max_tokens,
        )

        messages: list[ChatMessage] = []

        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})

        messages.append({"role": "user", "content": user_prompt})

        return self.chat(messages, temperature=temperature, max_tokens=max_tokens)

    # ============== 资源清理 ==============

    async def close_async(self) -> None:
        """
        异步关闭客户端，释放资源。

        使用 async with 语句时会自动调用此方法。
        """
        await self._client.aclose()
        logger.debug("LLMClient 已关闭: provider=%s", self.provider_id)

    def close(self) -> None:
        """
        同步关闭客户端，释放资源。

        注意：如果在异步上下文中调用此方法，会抛出 RuntimeError。
        此时应使用 close_async() 方法或使用 async with 语句。
        """
        self._run_sync(self.close_async())
