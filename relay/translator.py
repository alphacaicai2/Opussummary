"""
Opus Relay - 标题翻译模块

负责：
- 使用 OpenAI Compatible API 翻译文章标题为中文
- 翻译结果缓存（避免重复调用）
- 测试翻译连接
"""

import httpx
import logging
from database import get_cached_translation, save_translation

logger = logging.getLogger("opus.translator")


class Translator:
    """OpenAI Compatible 标题翻译器"""

    def __init__(self, base_url: str, api_key: str, model: str = "gpt-4o-mini"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.client = httpx.Client(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    def translate_title(self, title: str) -> str:
        """
        翻译单个标题为中文

        如果标题已经是中文，直接返回原标题。
        优先从缓存获取，未命中则调用 API。
        """
        if not title or not title.strip():
            return title

        # 简单判断是否已经是中文（包含中文字符超过一半）
        chinese_chars = sum(1 for c in title if '\u4e00' <= c <= '\u9fff')
        if chinese_chars > len(title) * 0.5:
            return title

        # 查缓存
        cached = get_cached_translation(title)
        if cached:
            logger.debug(f"翻译缓存命中: {title[:30]}...")
            return cached

        # 调用 API 翻译
        translated = self._call_api(title)
        if translated and translated != title:
            save_translation(title, translated)
            logger.info(f"翻译完成: {title[:30]}... → {translated[:30]}...")
        else:
            # 翻译失败或无变化，返回原标题
            translated = title

        return translated

    def translate_batch(self, titles: list[str]) -> list[str]:
        """
        批量翻译标题

        逐条翻译（有缓存兜底，不会太慢）
        """
        results = []
        for title in titles:
            results.append(self.translate_title(title))
        return results

    def _call_api(self, title: str) -> str | None:
        """调用 OpenAI Compatible API 翻译"""
        try:
            resp = self.client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是一个翻译助手。将给定的文章标题翻译成简体中文。"
                                "只返回翻译后的标题，不要添加任何解释或标点修改。"
                                "如果标题已经是中文，原样返回。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": title,
                        },
                    ],
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            translated = data["choices"][0]["message"]["content"].strip()
            # 去掉可能的引号包裹
            translated = translated.strip('"\'')
            return translated
        except httpx.HTTPError as e:
            logger.error(f"翻译 API 调用失败: {e}")
            return None
        except (KeyError, IndexError) as e:
            logger.error(f"翻译 API 响应解析失败: {e}")
            return None

    def test_connection(self) -> bool:
        """测试翻译 API 连接"""
        test_title = "OpenAI Releases GPT-5 with Breakthrough Capabilities"
        try:
            translated = self._call_api(test_title)
            if translated:
                print(f"✅ 翻译 API 连接成功！")
                print(f"   原文: {test_title}")
                print(f"   译文: {translated}")
                print(f"   模型: {self.model}")
                print(f"   端点: {self.base_url}")
                return True
            else:
                print(f"❌ 翻译 API 返回为空")
                return False
        except Exception as e:
            print(f"❌ 翻译 API 连接失败: {e}")
            return False

    def close(self):
        """关闭 HTTP 客户端"""
        self.client.close()
