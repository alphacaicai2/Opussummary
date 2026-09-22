"""
Opus Relay - 轮询调度器

负责：
- 定时轮询 Miniflux 获取新条目
- 处理去重、翻译、路由、入队
- 定时刷新聚合队列
- 定时清理过期数据
"""

import time
import logging
import threading
from miniflux_client import MinifluxClient
from discord_sender import DiscordSender
from translator import Translator
from database import is_entry_exists, is_url_exists_in_category, save_entry, cleanup_expired, save_last_poll_time, get_last_poll_time

logger = logging.getLogger("opus.scheduler")


class PollScheduler:
    """轮询调度器"""

    def __init__(self, config: dict):
        self.config = config
        self.poll_interval = config.get("poll_interval_minutes", 15) * 60  # 转为秒
        self.routes: dict[str, str] = config.get("routes", {})
        self.categories_with_content: set[str] = set(config.get("categories_with_content", []))
        self.content_summary_length = config.get("content_summary_length", 300)
        self._stop_event = threading.Event()
        self._reload_event = threading.Event()  # 用于中断 wait 以应用新间隔

        # 从数据库恢复上次轮询时间（服务重启后不丢失）
        self._last_poll_time: float | None = get_last_poll_time()
        if self._last_poll_time:
            logger.info(f"从数据库恢复上次轮询时间: {self._last_poll_time}")

        # 初始化各组件
        self.miniflux = MinifluxClient(
            base_url=config["miniflux_url"],
            api_token=config["miniflux_token"],
        )

        translation_cfg = config.get("translation", {})
        self.translator = Translator(
            base_url=translation_cfg.get("base_url", ""),
            api_key=translation_cfg.get("api_key", ""),
            model=translation_cfg.get("model", "gpt-4o-mini"),
        ) if translation_cfg.get("base_url") and translation_cfg.get("api_key") else None

        self.discord = DiscordSender(
            batch_interval=config.get("batch_interval_seconds", 120),
            batch_max=config.get("batch_max_items", 15),
            timezone=config.get("timezone", "UTC"),
            content_summary_length=self.content_summary_length,
        )

    def poll_once(self) -> int:
        """
        执行一次轮询

        只拉取时间窗口内的新文章（按 Miniflux 抓取时间过滤）：
        - 首次运行：拉取最近 2 小时内被抓取的文章
        - 后续运行：拉取上次轮询以来被抓取的文章

        返回本次新推送的条目数
        """
        now = time.time()

        # 计算时间窗口起点（按 changed_at 过滤，而非 published_at）
        if self._last_poll_time is None:
            # 首次运行，拉取最近 2 小时内被抓取的文章
            changed_after = int(now - 2 * 3600)  # 2 小时
            logger.info("首次轮询，拉取最近 2 小时内被抓取的文章...")
        else:
            # 后续运行，拉取上次轮询以来被抓取的文章
            changed_after = int(self._last_poll_time)
            logger.info("开始轮询 Miniflux（增量拉取）...")

        entries = self.miniflux.get_unread_entries(
            limit=100, changed_after=changed_after
        )

        # 更新轮询时间并持久化（放在拉取之后，确保不会遗漏）
        self._last_poll_time = now
        save_last_poll_time(now)

        if not entries:
            logger.info("没有新条目")
            return 0

        new_count = 0

        for entry in entries:
            entry_id = entry["id"]
            feed_id = str(entry.get("feed_id", ""))
            category_id = str(entry.get("feed", {}).get("category", {}).get("id", ""))

            # entry_id 去重
            if is_entry_exists(entry_id):
                continue

            # 分组内 URL 去重：同一分组内相同 URL 只推一次
            url = entry.get("url", "")
            cat_id_int = int(category_id) if category_id.isdigit() else 0
            if url and is_url_exists_in_category(url, cat_id_int):
                logger.debug(f"分组内重复跳过: category={category_id} url={url[:60]}")
                # 仍然记录 entry_id 避免下次重复检查
                save_entry(
                    entry_id=entry_id,
                    feed_id=int(feed_id) if feed_id.isdigit() else 0,
                    category_id=cat_id_int,
                    title=entry.get("title", ""),
                    title_zh="",
                    url=url,
                    published=entry.get("published_at", ""),
                )
                continue

            # 查找路由：feed_id > category_id > * 通配
            webhook_url = (
                self.routes.get(f"feed:{feed_id}")
                or self.routes.get(category_id)
                or self.routes.get("*")
            )
            if not webhook_url:
                logger.debug(f"category_id={category_id} feed_id={feed_id} 未配置路由，跳过")
                continue

            title = entry.get("title", "无标题")
            published = entry.get("published_at", "")
            feed_name = entry.get("feed", {}).get("title", "未知来源")

            # 翻译标题
            title_zh = title
            if self.translator:
                try:
                    title_zh = self.translator.translate_title(title)
                except Exception as e:
                    logger.error(f"翻译失败，使用原标题: {e}")

            # 根据 categories_with_content 决定是否获取 content
            content = ""
            if category_id in self.categories_with_content:
                try:
                    content = self.miniflux.get_entry_content(entry_id)
                    logger.debug(f"获取文章内容: entry_id={entry_id}, length={len(content)}")
                except Exception as e:
                    logger.warning(f"获取文章内容失败: entry_id={entry_id}, {e}")

            # 保存到数据库（去重 + 日报预留）
            save_entry(
                entry_id=entry_id,
                feed_id=int(feed_id) if feed_id.isdigit() else 0,
                category_id=cat_id_int,
                title=title,
                title_zh=title_zh,
                url=url,
                published=published,
                content=content,
            )

            # 加入 Discord 发送队列
            self.discord.enqueue(webhook_url, {
                "title": title,
                "title_zh": title_zh,
                "url": url,
                "feed_name": feed_name,
                "published": published,
                "content": content,
            })

            new_count += 1

        # 强制刷新所有队列（确保本次轮询的消息发出）
        self.discord.flush()

        logger.info(f"本次轮询完成：{new_count} 条新条目已推送")
        return new_count

    def run(self):
        """启动轮询循环"""
        logger.info(
            f"Opus Relay 启动！"
            f"轮询间隔: {self.poll_interval // 60} 分钟，"
            f"已配置 {len(self.routes)} 条路由"
        )

        # 启动时立即执行一次
        try:
            self.poll_once()
        except Exception as e:
            logger.error(f"首次轮询出错: {e}")

        # 启动聚合队列刷新线程
        flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True
        )
        flush_thread.start()

        # 启动清理线程
        cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True
        )
        cleanup_thread.start()

        # 主轮询循环
        while not self._stop_event.is_set():
            # 等待轮询间隔，每秒检查一次 stop/reload 信号
            self._reload_event.clear()
            waited = 0
            while waited < self.poll_interval:
                if self._stop_event.is_set() or self._reload_event.is_set():
                    break
                time.sleep(1)
                waited += 1
            if self._stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"轮询出错: {e}", exc_info=True)

    def _flush_loop(self):
        """定期刷新聚合队列"""
        while not self._stop_event.is_set():
            time.sleep(30)  # 每30秒检查一次
            try:
                self.discord.flush_expired()
            except Exception as e:
                logger.error(f"刷新队列出错: {e}")

    def _cleanup_loop(self):
        """定期清理过期数据（每6小时）"""
        while not self._stop_event.is_set():
            self._stop_event.wait(6 * 3600)  # 6小时
            if self._stop_event.is_set():
                break
            try:
                deleted_entries, deleted_cache = cleanup_expired()
                if deleted_entries or deleted_cache:
                    logger.info(
                        f"清理完成: {deleted_entries} 条过期条目, "
                        f"{deleted_cache} 条过期翻译缓存"
                    )
            except Exception as e:
                logger.error(f"清理出错: {e}")

    def stop(self):
        """停止调度器"""
        logger.info("正在停止 Opus Relay...")
        self._stop_event.set()
        self.discord.flush()  # 发送剩余队列
        self.discord.close()
        self.miniflux.close()
        if self.translator:
            self.translator.close()

    def reload(self, new_config: dict):
        """
        热重载配置（不中断轮询循环）

        支持更新：路由表、轮询间隔、翻译器配置
        """
        logger.info("🔄 正在热重载配置...")

        # 更新路由表
        old_routes = len(self.routes)
        self.routes = new_config.get("routes", {})
        logger.info(f"   路由: {old_routes} → {len(self.routes)} 条")

        # 更新轮询间隔
        old_interval = self.poll_interval
        self.poll_interval = new_config.get("poll_interval_minutes", 15) * 60
        if old_interval != self.poll_interval:
            logger.info(f"   轮询间隔: {old_interval // 60} → {self.poll_interval // 60} 分钟")
            # 唤醒轮询循环以使用新间隔（不会触发停止）
            self._reload_event.set()

        # 更新翻译器
        translation_cfg = new_config.get("translation", {})
        if translation_cfg.get("base_url") and translation_cfg.get("api_key"):
            if self.translator:
                self.translator.close()
            self.translator = Translator(
                base_url=translation_cfg.get("base_url", ""),
                api_key=translation_cfg.get("api_key", ""),
                model=translation_cfg.get("model", "gpt-4o-mini"),
            )
            logger.info(f"   翻译器: 已更新 (模型: {translation_cfg.get('model', 'gpt-4o-mini')})")
        elif not translation_cfg.get("base_url"):
            if self.translator:
                self.translator.close()
                self.translator = None
            logger.info("   翻译器: 已禁用")

        # 更新 Miniflux 客户端（如果 URL/Token 变了）
        if (new_config.get("miniflux_url") != self.config.get("miniflux_url") or
                new_config.get("miniflux_token") != self.config.get("miniflux_token")):
            self.miniflux.close()
            self.miniflux = MinifluxClient(
                base_url=new_config["miniflux_url"],
                api_token=new_config["miniflux_token"],
            )
            logger.info("   Miniflux 客户端: 已重新连接")

        # 更新时区（如果变了）
        old_tz = self.config.get("timezone", "UTC")
        new_tz = new_config.get("timezone", "UTC")
        if old_tz != new_tz:
            from zoneinfo import ZoneInfo
            try:
                self.discord.user_tz = ZoneInfo(new_tz)
                logger.info(f"   时区: {old_tz} → {new_tz}")
            except Exception as e:
                logger.warning(f"   时区更新失败: {e}")

        # 更新 categories_with_content
        old_categories = len(self.categories_with_content)
        self.categories_with_content = set(new_config.get("categories_with_content", []))
        if old_categories != len(self.categories_with_content):
            logger.info(f"   获取正文的分组: {old_categories} → {len(self.categories_with_content)} 个")

        # 更新 content_summary_length
        old_length = self.content_summary_length
        self.content_summary_length = new_config.get("content_summary_length", 300)
        self.discord.content_summary_length = self.content_summary_length
        if old_length != self.content_summary_length:
            logger.info(f"   摘要长度: {old_length} → {self.content_summary_length}")

        self.config = new_config
        logger.info("✅ 配置热重载完成！")
