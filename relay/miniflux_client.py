"""
Opus Relay - Miniflux API 客户端

负责：
- 从 Miniflux 拉取未读条目
- 获取 Feed 和 Category 信息
"""

import httpx
import logging

logger = logging.getLogger("opus.miniflux")


class MinifluxClient:
    """Miniflux API 客户端"""

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Auth-Token": api_token}
        self.client = httpx.Client(
            base_url=self.base_url,
            headers=self.headers,
            timeout=30.0,
        )

    def get_unread_entries(self, limit: int = 100,
                           changed_after: int | None = None) -> list[dict]:
        """
        获取未读条目

        Args:
            limit: 最多返回条数
            changed_after: Unix 时间戳，只返回此时间之后被 Miniflux 抓取/更新的条目

        返回格式：
        [
            {
                "id": 12345,
                "feed_id": 1,
                "title": "...",
                "url": "...",
                "published_at": "2026-02-19T...",  # 文章原始发布时间（用于显示）
                "changed_at": "2026-02-19T...",    # Miniflux 抓取时间（用于过滤）
                "feed": {"title": "...", "category": {"id": 2, ...}},
                ...
            }
        ]
        """
        try:
            params = {
                "status": "unread",
                "limit": limit,
                "order": "published_at",
                "direction": "desc",
            }
            if changed_after is not None:
                params["changed_after"] = changed_after

            resp = self.client.get("/v1/entries", params=params)
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("entries", [])
            logger.info(f"从 Miniflux 拉取到 {len(entries)} 条未读条目")
            return entries
        except httpx.HTTPError as e:
            logger.error(f"拉取 Miniflux 条目失败: {e}")
            return []

    def get_feeds(self) -> list[dict]:
        """获取所有 Feed 列表"""
        try:
            resp = self.client.get("/v1/feeds")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"获取 Feed 列表失败: {e}")
            return []

    def get_categories(self) -> list[dict]:
        """获取所有分组"""
        try:
            resp = self.client.get("/v1/categories")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"获取分组列表失败: {e}")
            return []

    def get_entry_content(self, entry_id: int) -> str:
        """
        获取单条文章的完整内容

        Args:
            entry_id: 文章 ID

        Returns:
            文章内容（HTML 格式），失败返回空字符串
        """
        try:
            resp = self.client.get(f"/v1/entries/{entry_id}")
            resp.raise_for_status()
            data = resp.json()
            return data.get("content", "")
        except httpx.HTTPError as e:
            logger.error(f"获取文章内容失败 (entry_id={entry_id}): {e}")
            return ""

    def mark_as_read(self, entry_ids: list[int]):
        """将条目标记为已读"""
        if not entry_ids:
            return
        try:
            resp = self.client.put("/v1/entries", json={
                "entry_ids": entry_ids,
                "status": "read",
            })
            resp.raise_for_status()
            logger.info(f"已将 {len(entry_ids)} 条条目标记为已读")
        except httpx.HTTPError as e:
            logger.error(f"标记已读失败: {e}")

    def test_connection(self) -> bool:
        """测试 Miniflux 连接"""
        try:
            resp = self.client.get("/v1/me")
            resp.raise_for_status()
            user = resp.json()
            logger.info(f"Miniflux 连接成功，用户: {user.get('username')}")
            return True
        except httpx.HTTPError as e:
            logger.error(f"Miniflux 连接失败: {e}")
            return False

    def close(self):
        """关闭 HTTP 客户端"""
        self.client.close()
