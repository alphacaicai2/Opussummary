"""
OpusBrief SQLite 数据库管理模块

功能覆盖：
1. 数据库初始化（data/briefings.db）
2. WAL 并发模式与连接上下文管理
3. 核心表 CRUD 操作
4. 简报与素材读写接口
5. 完整的类型注解和错误处理

作者: OpusBrief Team
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence, Union

# 配置日志
LOGGER = logging.getLogger("opus.database")

# 数据库路径配置
DB_PATH = Path("data") / "briefings.db"

# SQLite 连接配置
SQLITE_TIMEOUT_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MS = 5000

# 数据库 Schema 定义
SCHEMA_SQL = """
-- LLM 配置
CREATE TABLE IF NOT EXISTS llm_configs (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    provider    TEXT,
    base_url    TEXT,
    api_key     TEXT,
    model       TEXT,
    is_default  BOOLEAN DEFAULT 0,
    created_at  TEXT
);

-- Webhook 配置
CREATE TABLE IF NOT EXISTS webhooks (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    type        TEXT,
    url         TEXT,
    is_default  BOOLEAN DEFAULT 0,
    created_at  TEXT
);

-- 简报任务
CREATE TABLE IF NOT EXISTS briefing_tasks (
    id             INTEGER PRIMARY KEY,
    name           TEXT,
    template       TEXT,
    schedule       TEXT,
    timezone       TEXT,
    categories     TEXT,
    time_range     INTEGER,
    llm_config_id  INTEGER,
    webhook_ids    TEXT,
    enabled        BOOLEAN DEFAULT 1,
    created_at     TEXT
);

-- 简报历史
CREATE TABLE IF NOT EXISTS briefings (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER,
    template      TEXT,
    content       TEXT,
    content_html  TEXT,
    article_count INTEGER,
    source_start  TEXT,
    source_end    TEXT,
    sent_to       TEXT,
    created_at    TEXT
);

-- 素材缓存
CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY,
    entry_id     INTEGER UNIQUE,
    category_id  INTEGER,
    title        TEXT,
    url          TEXT,
    published_at TEXT,
    content      TEXT,
    fetched_at   TEXT
);

-- 用户偏好
CREATE TABLE IF NOT EXISTS user_preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

-- 索引定义
CREATE INDEX IF NOT EXISTS idx_llm_configs_default
ON llm_configs (is_default);

CREATE INDEX IF NOT EXISTS idx_webhooks_default
ON webhooks (is_default);

-- 部分唯一索引：确保每个表只有一个默认配置
CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_configs_default_true
ON llm_configs (is_default) WHERE is_default = 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_webhooks_default_true
ON webhooks (is_default) WHERE is_default = 1;

CREATE INDEX IF NOT EXISTS idx_briefing_tasks_enabled
ON briefing_tasks (enabled);

CREATE INDEX IF NOT EXISTS idx_briefings_task_created_at
ON briefings (task_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_articles_category_published_at
ON articles (category_id, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_articles_published_at
ON articles (published_at DESC);

-- 注意：entry_id 已有 UNIQUE 约束，自带索引，无需额外创建
"""


class DatabaseError(RuntimeError):
    """数据库相关异常基类"""

    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error


class ValidationError(ValueError):
    """数据验证异常"""
    pass


# ==================== 数据模型定义 ====================

@dataclass(slots=True, frozen=True)
class LLMConfig:
    """LLM 配置数据模型"""
    id: int
    name: Optional[str]
    provider: Optional[str]
    base_url: Optional[str]
    api_key: Optional[str]
    model: Optional[str]
    is_default: bool
    created_at: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（不包含敏感信息）"""
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "is_default": self.is_default,
            "created_at": self.created_at,
        }


@dataclass(slots=True, frozen=True)
class Webhook:
    """Webhook 配置数据模型"""
    id: int
    name: Optional[str]
    type: Optional[str]
    url: Optional[str]
    is_default: bool
    created_at: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "is_default": self.is_default,
            "created_at": self.created_at,
        }


@dataclass(slots=True, frozen=True)
class BriefingTask:
    """简报任务数据模型"""
    id: int
    name: Optional[str]
    template: Optional[str]
    schedule: Optional[str]
    timezone: Optional[str]
    categories: Optional[str]
    time_range: Optional[int]
    llm_config_id: Optional[int]
    webhook_ids: Optional[str]
    enabled: bool
    created_at: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "template": self.template,
            "schedule": self.schedule,
            "timezone": self.timezone,
            "categories": self.categories,
            "time_range": self.time_range,
            "llm_config_id": self.llm_config_id,
            "webhook_ids": self.webhook_ids,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }

    def get_category_ids(self) -> list[int]:
        """解析并返回 category ID 列表（容错处理）"""
        if not self.categories:
            return []
        result: list[int] = []
        for x in self.categories.split(","):
            x = x.strip()
            if x:
                try:
                    result.append(int(x))
                except ValueError:
                    LOGGER.warning("Invalid category_id in task %d: %s", self.id, x)
        return result

    def get_webhook_ids(self) -> list[int]:
        """解析并返回 webhook ID 列表（容错处理）"""
        if not self.webhook_ids:
            return []
        result: list[int] = []
        for x in self.webhook_ids.split(","):
            x = x.strip()
            if x:
                try:
                    result.append(int(x))
                except ValueError:
                    LOGGER.warning("Invalid webhook_id in task %d: %s", self.id, x)
        return result


@dataclass(slots=True, frozen=True)
class Briefing:
    """简报历史数据模型"""
    id: int
    task_id: Optional[int]
    template: Optional[str]
    content: Optional[str]
    content_html: Optional[str]
    article_count: Optional[int]
    source_start: Optional[str]
    source_end: Optional[str]
    sent_to: Optional[str]
    created_at: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "template": self.template,
            "content": self.content,
            "content_html": self.content_html,
            "article_count": self.article_count,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "sent_to": self.sent_to,
            "created_at": self.created_at,
        }


@dataclass(slots=True, frozen=True)
class Article:
    """素材数据模型"""
    id: int
    entry_id: int
    category_id: Optional[int]
    title: Optional[str]
    url: Optional[str]
    published_at: Optional[str]
    content: Optional[str]
    fetched_at: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "entry_id": self.entry_id,
            "category_id": self.category_id,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "content": self.content,
            "fetched_at": self.fetched_at,
        }


@dataclass(slots=True)
class ArticlePayload:
    """素材写入载荷（可变）"""
    entry_id: int
    category_id: Optional[int] = None
    title: Optional[str] = None
    url: Optional[str] = None
    published_at: Optional[str] = None
    content: Optional[str] = None
    fetched_at: Optional[str] = None


@dataclass(slots=True, frozen=True)
class UserPreference:
    """用户偏好数据模型"""
    key: str
    value: Optional[str]
    updated_at: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "key": self.key,
            "value": self.value,
            "updated_at": self.updated_at,
        }


# ==================== 工具函数 ====================

def utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO8601 格式字符串（秒精度）"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_positive_int(name: str, value: int) -> None:
    """验证参数必须为正整数"""
    if not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{name} must be a positive integer, got {value!r}")


def _require_non_negative_int(name: str, value: int) -> None:
    """验证参数必须为非负整数"""
    if not isinstance(value, int) or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer, got {value!r}")


def _require_non_empty_str(name: str, value: str) -> None:
    """验证参数必须为非空字符串"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")


def _serialize_text_field(
    value: Union[str, Sequence[int], Sequence[str], None]
) -> str:
    """将字段序列化为文本（用于 categories, webhook_ids 等）"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return ",".join(str(item) for item in value)


def _to_optional_str(value: Any) -> Optional[str]:
    """将任意值转换为可选字符串"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _normalize_article_payload(
    item: Union[ArticlePayload, Mapping[str, Any]]
) -> ArticlePayload:
    """标准化素材载荷"""
    if isinstance(item, ArticlePayload):
        if item.entry_id <= 0:
            raise ValidationError(f"article.entry_id must be > 0, got {item.entry_id}")
        return item

    # 从字典转换
    try:
        entry_id = int(item["entry_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(
            f"invalid article payload, missing/invalid entry_id: {item!r}"
        ) from exc

    if entry_id <= 0:
        raise ValidationError(f"article.entry_id must be > 0, got {entry_id}")

    category_id_raw = item.get("category_id")
    category_id: Optional[int] = None
    if category_id_raw is not None and category_id_raw != "":
        try:
            category_id = int(category_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"invalid article payload, category_id must be int or null: {item!r}"
            ) from exc

    return ArticlePayload(
        entry_id=entry_id,
        category_id=category_id,
        title=_to_optional_str(item.get("title")),
        url=_to_optional_str(item.get("url")),
        published_at=_to_optional_str(item.get("published_at")),
        content=_to_optional_str(item.get("content")),
        fetched_at=_to_optional_str(item.get("fetched_at")),
    )


def _chunked(values: Sequence[int], chunk_size: int = 300) -> Iterator[Sequence[int]]:
    """将序列分块，用于批量 SQL 操作"""
    for start in range(0, len(values), chunk_size):
        yield values[start:start + chunk_size]


# ==================== 行转换函数 ====================

def _row_to_llm_config(row: sqlite3.Row) -> LLMConfig:
    """将数据库行转换为 LLMConfig 对象"""
    return LLMConfig(
        id=int(row["id"]),
        name=row["name"],
        provider=row["provider"],
        base_url=row["base_url"],
        api_key=row["api_key"],
        model=row["model"],
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
    )


def _row_to_webhook(row: sqlite3.Row) -> Webhook:
    """将数据库行转换为 Webhook 对象"""
    return Webhook(
        id=int(row["id"]),
        name=row["name"],
        type=row["type"],
        url=row["url"],
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
    )


def _row_to_briefing_task(row: sqlite3.Row) -> BriefingTask:
    """将数据库行转换为 BriefingTask 对象"""
    time_range_val = row["time_range"]
    return BriefingTask(
        id=int(row["id"]),
        name=row["name"],
        template=row["template"],
        schedule=row["schedule"],
        timezone=row["timezone"],
        categories=row["categories"],
        time_range=int(time_range_val) if time_range_val is not None else None,
        llm_config_id=row["llm_config_id"],
        webhook_ids=row["webhook_ids"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )


def _row_to_briefing(row: sqlite3.Row) -> Briefing:
    """将数据库行转换为 Briefing 对象"""
    article_count_val = row["article_count"]
    task_id_val = row["task_id"]
    return Briefing(
        id=int(row["id"]),
        task_id=int(task_id_val) if task_id_val is not None else None,
        template=row["template"],
        content=row["content"],
        content_html=row["content_html"],
        article_count=int(article_count_val) if article_count_val is not None else None,
        source_start=row["source_start"],
        source_end=row["source_end"],
        sent_to=row["sent_to"],
        created_at=row["created_at"],
    )


def _row_to_article(row: sqlite3.Row) -> Article:
    """将数据库行转换为 Article 对象"""
    category_id_val = row["category_id"]
    return Article(
        id=int(row["id"]),
        entry_id=int(row["entry_id"]),
        category_id=int(category_id_val) if category_id_val is not None else None,
        title=row["title"],
        url=row["url"],
        published_at=row["published_at"],
        content=row["content"],
        fetched_at=row["fetched_at"],
    )


# ==================== 数据库连接管理 ====================

@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """
    SQLite 连接上下文管理器

    特性：
    - 自动创建数据目录
    - 启用 WAL 模式提高并发性能
    - 启用外键约束
    - 自动提交/回滚事务
    - 自动关闭连接

    Yields:
        sqlite3.Connection: 数据库连接对象

    Raises:
        DatabaseError: 数据库操作失败时抛出
    """
    conn: Optional[sqlite3.Connection] = None

    try:
        # 确保数据目录存在
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        # 创建连接（使用默认的 isolation_level，自动管理事务）
        conn = sqlite3.connect(
            DB_PATH,
            timeout=SQLITE_TIMEOUT_SECONDS,
        )
        conn.row_factory = sqlite3.Row

        # 配置 PRAGMA
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")

        yield conn

        # 提交事务
        conn.commit()

    except sqlite3.Error as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass  # 忽略回滚失败
        LOGGER.exception("数据库操作失败: %s", exc)
        raise DatabaseError(f"Database operation failed: {exc}", exc) from exc

    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        LOGGER.exception("数据库操作发生未知错误: %s", exc)
        raise

    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass  # 忽略关闭失败


def init_db() -> None:
    """
    初始化数据库与表结构

    此函数幂等，多次调用不会产生副作用。
    如果数据库文件不存在，将自动创建。
    """
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
    LOGGER.info("数据库初始化完成: %s", DB_PATH.resolve())


# 为了向后兼容
init_database = init_db


# ==================== LLM 配置 CRUD ====================

def create_llm_config(
    *,
    name: str,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    is_default: bool = False,
    created_at: Optional[str] = None,
) -> int:
    """
    创建 LLM 配置

    Args:
        name: 配置名称
        provider: 提供商 ID (openai, anthropic, etc.)
        base_url: API 基础 URL
        api_key: API 密钥
        model: 模型名称
        is_default: 是否设为默认配置
        created_at: 创建时间（可选，默认当前 UTC 时间）

    Returns:
        int: 新创建记录的 ID

    Raises:
        ValidationError: 参数验证失败
        DatabaseError: 数据库操作失败
    """
    _require_non_empty_str("name", name)
    _require_non_empty_str("provider", provider)
    _require_non_empty_str("base_url", base_url)
    _require_non_empty_str("model", model)

    created_at = created_at or utc_now_iso()

    with get_connection() as conn:
        # 如果设置为默认，先清除其他默认标记
        if is_default:
            conn.execute("UPDATE llm_configs SET is_default = 0 WHERE is_default = 1;")

        cursor = conn.execute(
            """
            INSERT INTO llm_configs (
                name, provider, base_url, api_key, model, is_default, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (name, provider, base_url, api_key, model, int(is_default), created_at),
        )
        new_id = int(cursor.lastrowid)

    LOGGER.info("创建 LLM 配置成功: id=%d, name=%s", new_id, name)
    return new_id


def get_llm_config(config_id: int) -> Optional[LLMConfig]:
    """
    按 ID 获取 LLM 配置

    Args:
        config_id: 配置 ID

    Returns:
        LLMConfig | None: 配置对象，不存在则返回 None
    """
    _require_positive_int("config_id", config_id)

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, provider, base_url, api_key, model, is_default, created_at
            FROM llm_configs
            WHERE id = ?;
            """,
            (config_id,),
        ).fetchone()

    return _row_to_llm_config(row) if row else None


def list_llm_configs(*, limit: int = 100, offset: int = 0) -> list[LLMConfig]:
    """
    分页获取 LLM 配置列表

    Args:
        limit: 返回数量限制，默认 100
        offset: 偏移量，默认 0

    Returns:
        list[LLMConfig]: 配置列表
    """
    _require_positive_int("limit", limit)
    _require_non_negative_int("offset", offset)

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, provider, base_url, api_key, model, is_default, created_at
            FROM llm_configs
            ORDER BY is_default DESC, created_at DESC, id DESC
            LIMIT ? OFFSET ?;
            """,
            (limit, offset),
        ).fetchall()

    return [_row_to_llm_config(row) for row in rows]


def update_llm_config(
    config_id: int,
    *,
    name: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    is_default: Optional[bool] = None,
) -> bool:
    """
    更新 LLM 配置

    Args:
        config_id: 配置 ID
        name: 新名称（可选，非空）
        provider: 新提供商（可选，非空）
        base_url: 新基础 URL（可选，非空）
        api_key: 新 API 密钥（可选）
        model: 新模型名称（可选，非空）
        is_default: 是否为默认（可选）

    Returns:
        bool: 是否更新成功（如果有字段被更新且记录存在）

    Raises:
        ValidationError: 参数验证失败
    """
    _require_positive_int("config_id", config_id)

    updates: list[str] = []
    params: list[Any] = []

    if name is not None:
        _require_non_empty_str("name", name)
        updates.append("name = ?")
        params.append(name)
    if provider is not None:
        _require_non_empty_str("provider", provider)
        updates.append("provider = ?")
        params.append(provider)
    if base_url is not None:
        _require_non_empty_str("base_url", base_url)
        updates.append("base_url = ?")
        params.append(base_url)
    if api_key is not None:
        updates.append("api_key = ?")
        params.append(api_key)
    if model is not None:
        _require_non_empty_str("model", model)
        updates.append("model = ?")
        params.append(model)
    if is_default is not None:
        updates.append("is_default = ?")
        params.append(int(is_default))

    if not updates:
        return False

    with get_connection() as conn:
        # 如果设置为默认，先清除其他默认标记
        if is_default:
            conn.execute(
                "UPDATE llm_configs SET is_default = 0 WHERE id != ?;",
                (config_id,),
            )

        params.append(config_id)
        cursor = conn.execute(
            f"UPDATE llm_configs SET {', '.join(updates)} WHERE id = ?;",
            tuple(params),
        )
        success = cursor.rowcount > 0

    if success:
        LOGGER.info("更新 LLM 配置成功: id=%d", config_id)
    return success


def delete_llm_config(config_id: int) -> bool:
    """
    删除 LLM 配置

    Args:
        config_id: 配置 ID

    Returns:
        bool: 是否删除成功
    """
    _require_positive_int("config_id", config_id)

    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM llm_configs WHERE id = ?;", (config_id,))
        success = cursor.rowcount > 0

    if success:
        LOGGER.info("删除 LLM 配置成功: id=%d", config_id)
    return success


def get_default_llm_config() -> Optional[LLMConfig]:
    """
    获取默认 LLM 配置

    如果未设置默认配置，则返回第一条配置作为兜底。

    Returns:
        LLMConfig | None: 默认配置对象，不存在任何配置则返回 None
    """
    with get_connection() as conn:
        # 优先查找标记为默认的配置
        row = conn.execute(
            """
            SELECT id, name, provider, base_url, api_key, model, is_default, created_at
            FROM llm_configs
            WHERE is_default = 1
            ORDER BY id DESC
            LIMIT 1;
            """
        ).fetchone()

        # 如果没有默认配置，返回第一条配置作为兜底
        if row is None:
            row = conn.execute(
                """
                SELECT id, name, provider, base_url, api_key, model, is_default, created_at
                FROM llm_configs
                ORDER BY id ASC
                LIMIT 1;
                """
            ).fetchone()

    return _row_to_llm_config(row) if row else None


# ==================== Webhook CRUD ====================

def create_webhook(
    *,
    name: str,
    type: str,
    url: str,
    is_default: bool = False,
    created_at: Optional[str] = None,
) -> int:
    """
    创建 Webhook 配置

    Args:
        name: Webhook 名称
        type: Webhook 类型 (discord, wechat, etc.)
        url: Webhook URL
        is_default: 是否设为默认
        created_at: 创建时间（可选）

    Returns:
        int: 新记录 ID
    """
    _require_non_empty_str("name", name)
    _require_non_empty_str("type", type)
    _require_non_empty_str("url", url)

    created_at = created_at or utc_now_iso()

    with get_connection() as conn:
        if is_default:
            conn.execute("UPDATE webhooks SET is_default = 0 WHERE is_default = 1;")

        cursor = conn.execute(
            """
            INSERT INTO webhooks (name, type, url, is_default, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (name, type, url, int(is_default), created_at),
        )
        new_id = int(cursor.lastrowid)

    LOGGER.info("创建 Webhook 成功: id=%d, name=%s", new_id, name)
    return new_id


def get_webhook(webhook_id: int) -> Optional[Webhook]:
    """按 ID 获取 Webhook"""
    _require_positive_int("webhook_id", webhook_id)

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, type, url, is_default, created_at
            FROM webhooks
            WHERE id = ?;
            """,
            (webhook_id,),
        ).fetchone()

    return _row_to_webhook(row) if row else None


def list_webhooks(*, limit: int = 100, offset: int = 0) -> list[Webhook]:
    """分页获取 Webhook 列表"""
    _require_positive_int("limit", limit)
    _require_non_negative_int("offset", offset)

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, type, url, is_default, created_at
            FROM webhooks
            ORDER BY is_default DESC, created_at DESC, id DESC
            LIMIT ? OFFSET ?;
            """,
            (limit, offset),
        ).fetchall()

    return [_row_to_webhook(row) for row in rows]


def update_webhook(
    webhook_id: int,
    *,
    name: Optional[str] = None,
    type: Optional[str] = None,
    url: Optional[str] = None,
    is_default: Optional[bool] = None,
) -> bool:
    """
    更新 Webhook

    Args:
        webhook_id: Webhook ID
        name: 新名称（可选，非空）
        type: 新类型（可选，非空）
        url: 新 URL（可选，非空）
        is_default: 是否为默认（可选）

    Returns:
        bool: 是否更新成功

    Raises:
        ValidationError: 参数验证失败
    """
    _require_positive_int("webhook_id", webhook_id)

    updates: list[str] = []
    params: list[Any] = []

    if name is not None:
        _require_non_empty_str("name", name)
        updates.append("name = ?")
        params.append(name)
    if type is not None:
        _require_non_empty_str("type", type)
        updates.append("type = ?")
        params.append(type)
    if url is not None:
        _require_non_empty_str("url", url)
        updates.append("url = ?")
        params.append(url)
    if is_default is not None:
        updates.append("is_default = ?")
        params.append(int(is_default))

    if not updates:
        return False

    with get_connection() as conn:
        if is_default:
            conn.execute(
                "UPDATE webhooks SET is_default = 0 WHERE id != ?;",
                (webhook_id,),
            )

        params.append(webhook_id)
        cursor = conn.execute(
            f"UPDATE webhooks SET {', '.join(updates)} WHERE id = ?;",
            tuple(params),
        )
        return cursor.rowcount > 0


def delete_webhook(webhook_id: int) -> bool:
    """删除 Webhook"""
    _require_positive_int("webhook_id", webhook_id)

    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM webhooks WHERE id = ?;", (webhook_id,))
        return cursor.rowcount > 0


def get_default_webhook() -> Optional[Webhook]:
    """
    获取默认 Webhook

    如果未设置默认，则返回第一条作为兜底。
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, type, url, is_default, created_at
            FROM webhooks
            WHERE is_default = 1
            ORDER BY id DESC
            LIMIT 1;
            """
        ).fetchone()

        if row is None:
            row = conn.execute(
                """
                SELECT id, name, type, url, is_default, created_at
                FROM webhooks
                ORDER BY id ASC
                LIMIT 1;
                """
            ).fetchone()

    return _row_to_webhook(row) if row else None


# ==================== 简报任务 CRUD ====================

def create_briefing_task(
    *,
    name: str,
    template: str,
    schedule: str,
    timezone: str,
    categories: Union[str, Sequence[int], Sequence[str], None],
    time_range: int,
    llm_config_id: Optional[int],
    webhook_ids: Union[str, Sequence[int], Sequence[str], None],
    enabled: bool = True,
    created_at: Optional[str] = None,
) -> int:
    """创建简报任务"""
    _require_non_empty_str("name", name)
    _require_non_empty_str("template", template)
    _require_non_empty_str("schedule", schedule)
    _require_non_empty_str("timezone", timezone)
    _require_non_negative_int("time_range", time_range)

    if llm_config_id is not None:
        _require_positive_int("llm_config_id", llm_config_id)

    created_at = created_at or utc_now_iso()
    categories_text = _serialize_text_field(categories)
    webhook_ids_text = _serialize_text_field(webhook_ids)

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO briefing_tasks (
                name, template, schedule, timezone, categories,
                time_range, llm_config_id, webhook_ids, enabled, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                name,
                template,
                schedule,
                timezone,
                categories_text,
                time_range,
                llm_config_id,
                webhook_ids_text,
                int(enabled),
                created_at,
            ),
        )
        new_id = int(cursor.lastrowid)

    LOGGER.info("创建简报任务成功: id=%d, name=%s", new_id, name)
    return new_id


def get_briefing_task(task_id: int) -> Optional[BriefingTask]:
    """按 ID 获取简报任务"""
    _require_positive_int("task_id", task_id)

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                id, name, template, schedule, timezone, categories,
                time_range, llm_config_id, webhook_ids, enabled, created_at
            FROM briefing_tasks
            WHERE id = ?;
            """,
            (task_id,),
        ).fetchone()

    return _row_to_briefing_task(row) if row else None


def list_briefing_tasks(
    *,
    enabled: Optional[bool] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[BriefingTask]:
    """分页获取简报任务列表，可选按启用状态过滤"""
    _require_positive_int("limit", limit)
    _require_non_negative_int("offset", offset)

    where_clause = ""
    params: list[Any] = []

    if enabled is not None:
        where_clause = "WHERE enabled = ?"
        params.append(int(enabled))

    params.extend([limit, offset])

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, name, template, schedule, timezone, categories,
                time_range, llm_config_id, webhook_ids, enabled, created_at
            FROM briefing_tasks
            {where_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?;
            """,
            tuple(params),
        ).fetchall()

    return [_row_to_briefing_task(row) for row in rows]


def update_briefing_task(
    task_id: int,
    *,
    name: Optional[str] = None,
    template: Optional[str] = None,
    schedule: Optional[str] = None,
    timezone: Optional[str] = None,
    categories: Union[str, Sequence[int], Sequence[str], None] = None,
    time_range: Optional[int] = None,
    llm_config_id: Optional[int] = None,
    webhook_ids: Union[str, Sequence[int], Sequence[str], None] = None,
    enabled: Optional[bool] = None,
) -> bool:
    """更新简报任务"""
    _require_positive_int("task_id", task_id)

    updates: list[str] = []
    params: list[Any] = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if template is not None:
        updates.append("template = ?")
        params.append(template)
    if schedule is not None:
        updates.append("schedule = ?")
        params.append(schedule)
    if timezone is not None:
        updates.append("timezone = ?")
        params.append(timezone)
    if categories is not None:
        updates.append("categories = ?")
        params.append(_serialize_text_field(categories))
    if time_range is not None:
        _require_non_negative_int("time_range", time_range)
        updates.append("time_range = ?")
        params.append(time_range)
    if llm_config_id is not None:
        _require_positive_int("llm_config_id", llm_config_id)
        updates.append("llm_config_id = ?")
        params.append(llm_config_id)
    if webhook_ids is not None:
        updates.append("webhook_ids = ?")
        params.append(_serialize_text_field(webhook_ids))
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(int(enabled))

    if not updates:
        return False

    params.append(task_id)

    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE briefing_tasks SET {', '.join(updates)} WHERE id = ?;",
            tuple(params),
        )
        return cursor.rowcount > 0


def delete_briefing_task(task_id: int) -> bool:
    """删除简报任务"""
    _require_positive_int("task_id", task_id)

    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM briefing_tasks WHERE id = ?;", (task_id,))
        return cursor.rowcount > 0


# ==================== 简报历史 CRUD ====================

def save_briefing(
    *,
    task_id: Optional[int],
    template: str,
    content: str = "",
    content_html: str = "",
    article_count: int,
    source_start: Optional[str],
    source_end: Optional[str],
    sent_to: Optional[str],
    created_at: Optional[str] = None,
) -> int:
    """
    保存简报历史

    Args:
        task_id: 关联的任务 ID（可为 None，表示手动生成）
        template: 使用的模板 ID
        content: 简报文本内容
        content_html: 简报 HTML 内容
        article_count: 包含的素材数量
        source_start: 素材时间范围起始
        source_end: 素材时间范围结束
        sent_to: 发送目标描述
        created_at: 创建时间

    Returns:
        int: 新简报 ID

    Raises:
        ValidationError: 参数验证失败（content 和 content_html 不能同时为空）
    """
    if task_id is not None:
        _require_positive_int("task_id", task_id)
    _require_non_empty_str("template", template)
    _require_non_negative_int("article_count", article_count)

    # 至少需要 content 或 content_html 其中之一
    if not content and not content_html:
        raise ValidationError(
            "At least one of 'content' or 'content_html' must be provided"
        )

    created_at = created_at or utc_now_iso()

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO briefings (
                task_id, template, content, content_html, article_count,
                source_start, source_end, sent_to, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                task_id,
                template,
                content,
                content_html,
                article_count,
                source_start,
                source_end,
                sent_to,
                created_at,
            ),
        )
        new_id = int(cursor.lastrowid)

    LOGGER.info("保存简报成功: id=%d, template=%s", new_id, template)
    return new_id


def get_briefing(briefing_id: int) -> Optional[Briefing]:
    """按 ID 获取单条简报历史"""
    _require_positive_int("briefing_id", briefing_id)

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                id, task_id, template, content, content_html, article_count,
                source_start, source_end, sent_to, created_at
            FROM briefings
            WHERE id = ?;
            """,
            (briefing_id,),
        ).fetchone()

    return _row_to_briefing(row) if row else None


def get_briefings(
    *,
    task_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Briefing]:
    """
    获取简报历史列表

    Args:
        task_id: 按任务 ID 过滤（可选）
        limit: 返回数量限制
        offset: 偏移量

    Returns:
        list[Briefing]: 简报列表，按创建时间倒序
    """
    if task_id is not None:
        _require_positive_int("task_id", task_id)
    _require_positive_int("limit", limit)
    _require_non_negative_int("offset", offset)

    sql = """
        SELECT
            id, task_id, template, content, content_html, article_count,
            source_start, source_end, sent_to, created_at
        FROM briefings
    """
    params: list[Any] = []

    if task_id is not None:
        sql += " WHERE task_id = ?"
        params.append(task_id)

    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    return [_row_to_briefing(row) for row in rows]


def update_briefing_content(
    briefing_id: int,
    *,
    content: Optional[str] = None,
    content_html: Optional[str] = None,
    sent_to: Optional[str] = None,
) -> bool:
    """更新简报内容字段"""
    _require_positive_int("briefing_id", briefing_id)

    updates: list[str] = []
    params: list[Any] = []

    if content is not None:
        updates.append("content = ?")
        params.append(content)
    if content_html is not None:
        updates.append("content_html = ?")
        params.append(content_html)
    if sent_to is not None:
        updates.append("sent_to = ?")
        params.append(sent_to)

    if not updates:
        return False

    params.append(briefing_id)

    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE briefings SET {', '.join(updates)} WHERE id = ?;",
            tuple(params),
        )
        return cursor.rowcount > 0


def delete_briefing(briefing_id: int) -> bool:
    """删除简报历史"""
    _require_positive_int("briefing_id", briefing_id)

    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM briefings WHERE id = ?;", (briefing_id,))
        return cursor.rowcount > 0


# ==================== 素材缓存 CRUD ====================

def save_articles(
    articles: Sequence[Union[ArticlePayload, Mapping[str, Any]]],
) -> tuple[int, int]:
    """
    批量保存素材（UPSERT by entry_id）

    Args:
        articles: 素材列表，支持 ArticlePayload 对象或字典

    Returns:
        tuple[int, int]: (插入数量, 更新数量)
    """
    if not articles:
        return (0, 0)

    # 去重：同一批次重复 entry_id 以最后一个为准
    deduped: dict[int, ArticlePayload] = {}
    for raw_item in articles:
        item = _normalize_article_payload(raw_item)
        deduped[item.entry_id] = item

    normalized_items = list(deduped.values())
    entry_ids = [item.entry_id for item in normalized_items]

    # 分批查询已存在的 entry_id
    existing_ids: set[int] = set()
    with get_connection() as conn:
        for chunk in _chunked(entry_ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT entry_id FROM articles WHERE entry_id IN ({placeholders});",
                tuple(chunk),
            ).fetchall()
            existing_ids.update(int(row["entry_id"]) for row in rows)

        # 批量插入/更新
        params: list[tuple[Any, ...]] = []
        for item in normalized_items:
            fetched_at = item.fetched_at or utc_now_iso()
            params.append(
                (
                    item.entry_id,
                    item.category_id,
                    item.title,
                    item.url,
                    item.published_at,
                    item.content,
                    fetched_at,
                )
            )

        conn.executemany(
            """
            INSERT INTO articles (
                entry_id, category_id, title, url, published_at, content, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
                category_id = excluded.category_id,
                title = excluded.title,
                url = excluded.url,
                published_at = excluded.published_at,
                content = excluded.content,
                fetched_at = excluded.fetched_at;
            """,
            params,
        )

    inserted = sum(1 for entry_id in entry_ids if entry_id not in existing_ids)
    updated = len(entry_ids) - inserted

    LOGGER.info("保存素材完成: 插入 %d 条, 更新 %d 条", inserted, updated)
    return (inserted, updated)


def get_article_by_entry_id(entry_id: int) -> Optional[Article]:
    """按 entry_id 查询素材"""
    _require_positive_int("entry_id", entry_id)

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                id, entry_id, category_id, title, url, published_at, content, fetched_at
            FROM articles
            WHERE entry_id = ?;
            """,
            (entry_id,),
        ).fetchone()

    return _row_to_article(row) if row else None


def get_articles_by_categories(
    category_ids: Sequence[int],
    *,
    published_start: Optional[str] = None,
    published_end: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> list[Article]:
    """
    按分组查询素材

    Args:
        category_ids: 分组 ID 列表
        published_start: 发布时间起始（可选）
        published_end: 发布时间结束（可选）
        limit: 返回数量限制
        offset: 偏移量

    Returns:
        list[Article]: 素材列表，按发布时间倒序
    """
    if not category_ids:
        return []

    _require_positive_int("limit", limit)
    _require_non_negative_int("offset", offset)

    for category_id in category_ids:
        _require_positive_int("category_id", category_id)

    placeholders = ",".join("?" for _ in category_ids)
    sql = f"""
        SELECT
            id, entry_id, category_id, title, url, published_at, content, fetched_at
        FROM articles
        WHERE category_id IN ({placeholders})
    """
    params: list[Any] = list(category_ids)

    if published_start is not None:
        sql += " AND published_at >= ?"
        params.append(published_start)
    if published_end is not None:
        sql += " AND published_at <= ?"
        params.append(published_end)

    sql += " ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    return [_row_to_article(row) for row in rows]


def delete_articles_by_entry_ids(entry_ids: Sequence[int]) -> int:
    """
    按 entry_id 批量删除素材

    Args:
        entry_ids: 要删除的 entry_id 列表

    Returns:
        int: 删除的条数
    """
    if not entry_ids:
        return 0

    for entry_id in entry_ids:
        _require_positive_int("entry_id", entry_id)

    total_deleted = 0
    with get_connection() as conn:
        for chunk in _chunked(list(entry_ids)):
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"DELETE FROM articles WHERE entry_id IN ({placeholders});",
                tuple(chunk),
            )
            total_deleted += cursor.rowcount

    LOGGER.info("删除素材完成: %d 条", total_deleted)
    return total_deleted


# ==================== 用户偏好 CRUD ====================

def set_user_preference(key: str, value: str) -> None:
    """
    写入/更新用户偏好

    Args:
        key: 偏好键
        value: 偏好值
    """
    if not key or not key.strip():
        raise ValidationError("key must not be empty")

    now = utc_now_iso()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at;
            """,
            (key, value, now),
        )


def get_user_preference(key: str) -> Optional[UserPreference]:
    """按 key 获取用户偏好"""
    if not key or not key.strip():
        raise ValidationError("key must not be empty")

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT key, value, updated_at
            FROM user_preferences
            WHERE key = ?;
            """,
            (key,),
        ).fetchone()

    if row is None:
        return None

    return UserPreference(key=row["key"], value=row["value"], updated_at=row["updated_at"])


def list_user_preferences() -> list[UserPreference]:
    """获取全部用户偏好"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT key, value, updated_at
            FROM user_preferences
            ORDER BY key ASC;
            """
        ).fetchall()

    return [
        UserPreference(key=row["key"], value=row["value"], updated_at=row["updated_at"])
        for row in rows
    ]


def delete_user_preference(key: str) -> bool:
    """删除用户偏好"""
    if not key or not key.strip():
        raise ValidationError("key must not be empty")

    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM user_preferences WHERE key = ?;", (key,))
        return cursor.rowcount > 0


# ==================== 模块导出 ====================

__all__ = [
    # 常量
    "DB_PATH",
    # 异常
    "DatabaseError",
    "ValidationError",
    # 数据模型
    "LLMConfig",
    "Webhook",
    "BriefingTask",
    "Briefing",
    "Article",
    "ArticlePayload",
    "UserPreference",
    # 数据库连接
    "get_connection",
    "init_db",
    "init_database",  # 别名
    # 工具函数
    "utc_now_iso",
    # LLM 配置 CRUD
    "create_llm_config",
    "get_llm_config",
    "list_llm_configs",
    "update_llm_config",
    "delete_llm_config",
    "get_default_llm_config",
    # Webhook CRUD
    "create_webhook",
    "get_webhook",
    "list_webhooks",
    "update_webhook",
    "delete_webhook",
    "get_default_webhook",
    # 简报任务 CRUD
    "create_briefing_task",
    "get_briefing_task",
    "list_briefing_tasks",
    "update_briefing_task",
    "delete_briefing_task",
    # 简报历史 CRUD
    "save_briefing",
    "get_briefing",
    "get_briefings",
    "update_briefing_content",
    "delete_briefing",
    # 素材缓存 CRUD
    "save_articles",
    "get_article_by_entry_id",
    "get_articles_by_categories",
    "delete_articles_by_entry_ids",
    # 用户偏好 CRUD
    "set_user_preference",
    "get_user_preference",
    "list_user_preferences",
    "delete_user_preference",
]


# ==================== 模块测试入口 ====================

if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # 初始化数据库
    print("初始化数据库...")
    init_db()
    print(f"数据库文件位置: {DB_PATH.resolve()}")

    # 测试创建 LLM 配置
    print("\n测试创建 LLM 配置...")
    llm_id = create_llm_config(
        name="测试 OpenAI",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key="sk-test-key",
        model="gpt-4o",
        is_default=True,
    )
    print(f"创建 LLM 配置成功, ID: {llm_id}")

    # 测试获取默认配置
    default_llm = get_default_llm_config()
    if default_llm:
        print(f"默认 LLM 配置: {default_llm.name} ({default_llm.model})")

    # 测试创建 Webhook
    print("\n测试创建 Webhook...")
    webhook_id = create_webhook(
        name="测试 Discord",
        type="discord",
        url="https://discord.com/api/webhooks/test",
        is_default=True,
    )
    print(f"创建 Webhook 成功, ID: {webhook_id}")

    # 测试获取默认 Webhook
    default_webhook = get_default_webhook()
    if default_webhook:
        print(f"默认 Webhook: {default_webhook.name} ({default_webhook.type})")

    # 测试保存素材
    print("\n测试保存素材...")
    inserted, updated = save_articles([
        {
            "entry_id": 1001,
            "category_id": 1,
            "title": "测试文章 1",
            "url": "https://example.com/1",
            "published_at": "2026-02-20T10:00:00+00:00",
            "content": "这是测试内容",
        },
        {
            "entry_id": 1002,
            "category_id": 1,
            "title": "测试文章 2",
            "url": "https://example.com/2",
        },
    ])
    print(f"保存素材: 插入 {inserted}, 更新 {updated}")

    # 测试按分组查询素材
    articles = get_articles_by_categories([1], limit=10)
    print(f"查询到 {len(articles)} 条素材")
    for article in articles:
        print(f"  - {article.title}")

    # 测试保存简报
    print("\n测试保存简报...")
    briefing_id = save_briefing(
        task_id=None,
        template="general",
        content="# 测试简报\n\n这是测试内容。",
        content_html="<h1>测试简报</h1><p>这是测试内容。</p>",
        article_count=2,
        source_start="2026-02-20T00:00:00+00:00",
        source_end="2026-02-20T23:59:59+00:00",
        sent_to="Discord",
    )
    print(f"保存简报成功, ID: {briefing_id}")

    # 测试获取简报列表
    briefings = get_briefings(limit=10)
    print(f"查询到 {len(briefings)} 条简报")

    # 测试用户偏好
    print("\n测试用户偏好...")
    set_user_preference("timezone", "Asia/Shanghai")
    pref = get_user_preference("timezone")
    if pref:
        print(f"用户偏好 timezone = {pref.value}")

    print("\n所有测试完成!")
