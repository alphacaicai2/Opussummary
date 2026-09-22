"""
Opus Relay - SQLite 数据库操作模块

负责：
- 文章去重（entry_id + 分组内 URL 去重）
- 文章索引存储（为日报预留）
- 翻译缓存
- 7天自动过期清理
"""

import sqlite3
import os
from datetime import datetime, timedelta
from contextlib import contextmanager


# 数据库路径
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "opus.db")


def _ensure_dir():
    """确保 data 目录存在"""
    os.makedirs(DB_DIR, exist_ok=True)


@contextmanager
def get_conn():
    """获取数据库连接的上下文管理器"""
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """初始化数据库表"""
    with get_conn() as conn:
        # 文章索引表（去重 + 为日报预留）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                entry_id    INTEGER PRIMARY KEY,
                feed_id     INTEGER,
                category_id INTEGER,
                title       TEXT,
                title_zh    TEXT,
                url         TEXT,
                published   TEXT,
                content     TEXT,
                pushed_at   TEXT DEFAULT (datetime('now')),
                expires_at  TEXT
            )
        """)

        # 兼容旧表：如果 category_id 列不存在则添加
        try:
            conn.execute("SELECT category_id FROM entries LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE entries ADD COLUMN category_id INTEGER")

        # 兼容旧表：如果 content 列不存在则添加
        try:
            conn.execute("SELECT content FROM entries LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE entries ADD COLUMN content TEXT")

        # 翻译缓存表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS translation_cache (
                original    TEXT PRIMARY KEY,
                translated  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)

        # 状态存储表（key-value）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key     TEXT PRIMARY KEY,
                value   TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # 索引加速查询
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entries_feed_id
            ON entries(feed_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entries_expires
            ON entries(expires_at)
        """)
        # 分组内 URL 去重索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entries_cat_url
            ON entries(category_id, url)
        """)


def is_entry_exists(entry_id: int) -> bool:
    """检查 entry_id 是否已存在（去重）"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        return row is not None


def is_url_exists_in_category(url: str, category_id: int) -> bool:
    """检查同一分组内是否已推送过相同 URL（分组内去重）"""
    if not url:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM entries WHERE category_id = ? AND url = ?",
            (category_id, url)
        ).fetchone()
        return row is not None


def save_entry(entry_id: int, feed_id: int, category_id: int,
               title: str, title_zh: str, url: str, published: str,
               content: str = ""):
    """保存文章索引"""
    expires_at = (datetime.utcnow() + timedelta(days=7)).isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO entries
            (entry_id, feed_id, category_id, title, title_zh, url, published, content, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (entry_id, feed_id, category_id, title, title_zh, url, published, content, expires_at))


def get_cached_translation(original: str) -> str | None:
    """从缓存获取翻译结果"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT translated FROM translation_cache WHERE original = ?",
            (original,)
        ).fetchone()
        return row["translated"] if row else None


def save_translation(original: str, translated: str):
    """保存翻译结果到缓存"""
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO translation_cache (original, translated)
            VALUES (?, ?)
        """, (original, translated))


def cleanup_expired():
    """清理过期数据（7天前的条目 + 30天前的翻译缓存）"""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        deleted_entries = conn.execute(
            "DELETE FROM entries WHERE expires_at < ?", (now,)
        ).rowcount

        # 翻译缓存保留30天
        cache_cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
        deleted_cache = conn.execute(
            "DELETE FROM translation_cache WHERE created_at < ?",
            (cache_cutoff,)
        ).rowcount

        return deleted_entries, deleted_cache


def get_entries_since(hours: int = 24) -> list[dict]:
    """获取最近 N 小时的文章（为日报预留）"""
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE pushed_at >= ? ORDER BY published DESC",
            (since,)
        ).fetchall()
        return [dict(row) for row in rows]


def save_last_poll_time(timestamp: float):
    """保存上次轮询时间（Unix 时间戳）"""
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO state (key, value, updated_at)
            VALUES ('last_poll_time', ?, datetime('now'))
        """, (str(timestamp),))


def get_last_poll_time() -> float | None:
    """获取上次轮询时间（Unix 时间戳）"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM state WHERE key = 'last_poll_time'"
        ).fetchone()
        if row:
            try:
                return float(row["value"])
            except ValueError:
                return None
        return None
