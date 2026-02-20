"""
Opus Relay - Discord 推送模块

负责：
- 批量聚合推送（120s / 15条合一条消息）
- Discord Webhook 发送
- 429 速率限制处理（retry_after 队列）
"""

import httpx
import time
import logging
import threading
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict

logger = logging.getLogger("opus.discord")


class DiscordSender:
    """Discord Webhook 发送器，支持批量聚合和速率限制"""

    def __init__(self, batch_interval: int = 120, batch_max: int = 15,
                 timezone: str = "UTC", content_summary_length: int = 300):
        """
        Args:
            batch_interval: 聚合间隔（秒），达到此时间强制发送
            batch_max: 聚合上限（条），达到此数量立即发送
            timezone: 用户时区（如 "Asia/Shanghai", "America/New_York"）
            content_summary_length: 正文摘要长度（字符数）
        """
        self.batch_interval = batch_interval
        self.batch_max = batch_max
        self.content_summary_length = content_summary_length
        self.client = httpx.Client(timeout=30.0)

        # 设置用户时区
        try:
            self.user_tz = ZoneInfo(timezone)
        except Exception:
            logger.warning(f"无效时区 '{timezone}'，使用 UTC")
            self.user_tz = timezone.utc

        # 按 webhook_url 分组的待发队列
        # { webhook_url: [{"title": ..., "title_zh": ..., "url": ..., "feed_name": ...}] }
        self._queues: dict[str, list[dict]] = defaultdict(list)
        self._queue_timestamps: dict[str, float] = {}  # 每个队列的首条入队时间
        self._lock = threading.Lock()

    def _format_published_time(self, published_str: str) -> tuple[str, bool]:
        """
        将 ISO 8601 时间字符串转换为用户时区的友好格式

        Args:
            published_str: ISO 8601 格式时间（如 "2026-02-19T04:00:00Z"）

        Returns:
            (格式化的时间字符串, 是否有时间)
            如 ("02/19 12:00", True) 或 ("", False)
        """
        if not published_str:
            return ("", False)

        try:
            # 解析 ISO 8601 时间
            dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))

            # 确保有 timezone 信息
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            # 转换为用户时区
            local_dt = dt.astimezone(self.user_tz)

            # 格式化为友好格式
            return (local_dt.strftime("%m/%d %H:%M"), True)
        except Exception as e:
            logger.debug(f"解析时间失败: {published_str}, {e}")
            return ("", False)

    def _truncate_content(self, content: str, max_length: int = None) -> str:
        """
        截取正文摘要，清理 HTML 标签

        Args:
            content: 原始内容（可能包含 HTML）
            max_length: 最大长度，默认使用 content_summary_length

        Returns:
            清理并截断后的纯文本摘要
        """
        if not content:
            return ""

        if max_length is None:
            max_length = self.content_summary_length

        # 移除 HTML 标签
        text = re.sub(r'<[^>]+>', '', content)

        # 移除多余空白
        text = re.sub(r'\s+', ' ', text).strip()

        # 截断
        if len(text) > max_length:
            text = text[:max_length].rstrip() + "..."

        return text

    def enqueue(self, webhook_url: str, item: dict):
        """
        将一条消息加入待发队列

        item 格式：
        {
            "title": "原始标题",
            "title_zh": "中文标题",
            "url": "链接",
            "feed_name": "来源名称",
            "published": "发布时间",
            "content": "文章正文（可选）"
        }
        """
        with self._lock:
            if webhook_url not in self._queue_timestamps:
                self._queue_timestamps[webhook_url] = time.time()
            self._queues[webhook_url].append(item)

            # 达到批量上限，立即发送
            if len(self._queues[webhook_url]) >= self.batch_max:
                items = self._queues.pop(webhook_url)
                self._queue_timestamps.pop(webhook_url, None)
                self._send_batch(webhook_url, items)

    def flush(self):
        """强制发送所有队列中的待发消息"""
        with self._lock:
            for webhook_url in list(self._queues.keys()):
                items = self._queues.pop(webhook_url)
                self._queue_timestamps.pop(webhook_url, None)
                if items:
                    self._send_batch(webhook_url, items)

    def flush_expired(self):
        """发送超过聚合间隔的队列"""
        now = time.time()
        with self._lock:
            for webhook_url in list(self._queue_timestamps.keys()):
                if now - self._queue_timestamps[webhook_url] >= self.batch_interval:
                    items = self._queues.pop(webhook_url, [])
                    self._queue_timestamps.pop(webhook_url, None)
                    if items:
                        self._send_batch(webhook_url, items)

    def _send_batch(self, webhook_url: str, items: list[dict]):
        """将一批条目格式化为 Discord 消息并发送"""
        if not items:
            return

        # 构建 Discord Embed 消息
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        description_lines = []

        for i, item in enumerate(items, 1):
            title_zh = item.get("title_zh", "")
            title = item.get("title", "")
            url = item.get("url", "")
            feed_name = item.get("feed_name", "")
            published = item.get("published", "")
            content = item.get("content", "")

            # 格式化发布时间
            time_str, has_time = self._format_published_time(published)
            time_display = f"🕐 {time_str}" if has_time else "🕐 原始文章没发布时间"

            # 显示格式：中文标题（有的话）+ 原标题 + 链接 + 时间 + 正文摘要
            if title_zh and title_zh != title:
                line = f"**{i}.** [{title_zh}]({url})\n　　_{title}_ | {feed_name} | {time_display}"
            else:
                line = f"**{i}.** [{title}]({url})\n　　{feed_name} | {time_display}"

            # 添加正文摘要（如果有）
            if content:
                summary = self._truncate_content(content)
                if summary:
                    line += f"\n　　📝 {summary}"

            description_lines.append(line)

        description = "\n\n".join(description_lines)

        # Discord Embed 有 4096 字符限制，超长则分段发送
        if len(description) > 4000:
            self._send_chunked(webhook_url, items, now_str)
            return

        payload = {
            "embeds": [
                {
                    "title": f"📰 新文章推送 ({len(items)} 条)",
                    "description": description,
                    "color": 0x5865F2,  # Discord 蓝
                    "footer": {"text": f"Opus Relay | {now_str}"},
                }
            ]
        }

        self._send_webhook(webhook_url, payload)

    def _send_chunked(self, webhook_url: str, items: list[dict], now_str: str):
        """超长内容分段发送"""
        chunk_size = 10  # 每段最多10条
        for i in range(0, len(items), chunk_size):
            chunk = items[i:i + chunk_size]
            part = f"({i // chunk_size + 1}/{(len(items) - 1) // chunk_size + 1})"

            description_lines = []
            for j, item in enumerate(chunk, i + 1):
                title_zh = item.get("title_zh", "")
                title = item.get("title", "")
                url = item.get("url", "")
                feed_name = item.get("feed_name", "")
                published = item.get("published", "")
                content = item.get("content", "")

                # 格式化发布时间
                time_str, has_time = self._format_published_time(published)
                time_display = f"🕐 {time_str}" if has_time else "🕐 原始文章没发布时间"

                if title_zh and title_zh != title:
                    line = f"**{j}.** [{title_zh}]({url})\n　　_{title}_ | {feed_name} | {time_display}"
                else:
                    line = f"**{j}.** [{title}]({url})\n　　{feed_name} | {time_display}"

                # 添加正文摘要（如果有）
                if content:
                    summary = self._truncate_content(content)
                    if summary:
                        line += f"\n　　📝 {summary}"

                description_lines.append(line)

            payload = {
                "embeds": [
                    {
                        "title": f"📰 新文章推送 {part}",
                        "description": "\n\n".join(description_lines),
                        "color": 0x5865F2,
                        "footer": {"text": f"Opus Relay | {now_str}"},
                    }
                ]
            }
            self._send_webhook(webhook_url, payload)
            time.sleep(1)  # 分段之间稍作延迟

    def _send_webhook(self, webhook_url: str, payload: dict, max_retries: int = 3):
        """
        发送 Discord Webhook，处理 429 速率限制

        遇到 429 时读取 retry_after 并等待后重试
        """
        for attempt in range(max_retries):
            try:
                resp = self.client.post(webhook_url, json=payload)

                if resp.status_code == 204:
                    logger.info(f"Discord 推送成功")
                    return True

                if resp.status_code == 429:
                    # 速率限制，读取 retry_after
                    data = resp.json()
                    retry_after = data.get("retry_after", 5)
                    logger.warning(
                        f"Discord 429 速率限制，等待 {retry_after}s 后重试 "
                        f"(尝试 {attempt + 1}/{max_retries})"
                    )
                    time.sleep(retry_after)
                    continue

                # 其他错误
                logger.error(
                    f"Discord 推送失败: HTTP {resp.status_code} - {resp.text[:200]}"
                )
                return False

            except httpx.HTTPError as e:
                logger.error(f"Discord 请求异常: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避

        logger.error(f"Discord 推送重试耗尽")
        return False

    def send_single(self, webhook_url: str, title: str, title_zh: str,
                    url: str, feed_name: str):
        """直接发送单条消息（绕过聚合，用于测试）"""
        if title_zh and title_zh != title:
            description = f"[{title_zh}]({url})\n_{title}_\n\n来源: {feed_name}"
        else:
            description = f"[{title}]({url})\n\n来源: {feed_name}"

        payload = {
            "embeds": [
                {
                    "title": "📰 新文章",
                    "description": description,
                    "color": 0x5865F2,
                }
            ]
        }
        return self._send_webhook(webhook_url, payload)

    def close(self):
        """关闭 HTTP 客户端"""
        self.client.close()
