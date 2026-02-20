"""
OpusBrief Web Server

FastAPI-based REST API server for managing:
- Miniflux connection settings
- LLM provider configurations
- Webhook notification endpoints
- Briefing task scheduling
- Briefing generation and history
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional

import httpx
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from briefing.generator import BriefingGenerateRequest, BriefingGenerator
from briefing.templates import (
    BriefingTemplate,
    get_prompt_template,
    list_prompt_templates,
    PromptTemplate,
)
from llm.client import LLMClient
from miniflux_client import MinifluxClient
from sender.discord import DiscordSender, DiscordSenderError
from briefing.scheduler import BriefingScheduler

# =============================================================================
# Configuration & Constants
# =============================================================================

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "briefings.db"
ENV_PATH = ROOT_DIR / ".env"
STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger("opus.web")
_BOOTSTRAPPED = False

# LLM Provider presets
LLM_PROVIDERS = {
    "openai": {"base_url": "https://api.openai.com/v1", "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"]},
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "models": ["claude-3-5-sonnet", "claude-3-opus"]},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "models": ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"]},
    "zhipu_cn": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ["glm-4-plus", "glm-4-flash"]},
    "zhipu_us": {"base_url": "https://open.bigmodel.us/api/paas/v4", "models": ["glm-4-plus", "glm-4-flash"]},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"]},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"]},
}

# Briefing templates
BRIEFING_TEMPLATES = {
    "general": {"name": "通用格式", "description": "FA 日常信息流，融资动态 + AI 产品 + 二级市场"},
    "investment": {"name": "投融资格式", "description": "融资情报，轮次/金额/投资方/赛道，表格化"},
    "ai_product": {"name": "AI 产品格式", "description": "产品情报，模型发布、新产品、赛道探索、大厂动向"},
    "wechat_mp": {"name": "公众号格式", "description": "创作灵感，焦虑点/共鸣点/争议点/爆款潜力"},
}


# =============================================================================
# Utility Functions
# =============================================================================

def _utcnow_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    """Parse ISO format string to datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_dotenv(path: Path) -> None:
    """Load environment variables from .env file."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _json_dumps(data: Any) -> str:
    """Serialize data to JSON string."""
    return json.dumps(data, ensure_ascii=False)


def _json_loads(value: str | None, fallback: Any) -> Any:
    """Deserialize JSON string to data."""
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _validate_url_for_ssrf(url: str, allow_private: bool = False) -> tuple[bool, str]:
    """
    Validate a URL to prevent Server-Side Request Forgery (SSRF) attacks.

    Args:
        url: The URL to validate
        allow_private: Whether to allow private/internal IP addresses (default: False)

    Returns:
        Tuple of (is_valid, error_message)
    """
    from urllib.parse import urlparse
    import ipaddress
    import socket

    if not url:
        return False, "URL is empty"

    try:
        parsed = urlparse(url)

        # Must be http or https
        if parsed.scheme not in ("http", "https"):
            return False, f"Invalid URL scheme: {parsed.scheme}. Only http and https are allowed."

        # Must have a hostname
        hostname = parsed.hostname
        if not hostname:
            return False, "URL must have a hostname"

        # Block dangerous schemes that might bypass checks
        if "@" in url:
            return False, "URL contains invalid characters"

        if not allow_private:
            # Resolve hostname to IP and check if it's private/internal
            try:
                # Check for localhost variations
                if hostname.lower() in ("localhost", "localhost.localdomain"):
                    return False, "Access to localhost is not allowed"

                # Resolve the hostname
                ip_str = socket.gethostbyname(hostname)
                ip = ipaddress.ip_address(ip_str)

                # Block private/internal IP ranges
                if ip.is_private:
                    return False, "Access to private IP addresses is not allowed"
                if ip.is_loopback:
                    return False, "Access to loopback addresses is not allowed"
                if ip.is_link_local:
                    return False, "Access to link-local addresses is not allowed"
                if ip.is_multicast:
                    return False, "Access to multicast addresses is not allowed"
                if ip.is_reserved:
                    return False, "Access to reserved IP addresses is not allowed"

            except socket.gaierror:
                # DNS resolution failed - could be a bad domain
                return False, f"Could not resolve hostname: {hostname}"
            except ValueError:
                return False, f"Invalid IP address for hostname: {hostname}"

        return True, ""

    except Exception as e:
        return False, f"URL validation error: {str(e)}"


# =============================================================================
# Database Layer
# =============================================================================

@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    """Get database connection with row factory."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _init_db() -> None:
    """Initialize database schema."""
    ddl = """
    -- System-wide settings (Miniflux connection, etc.)
    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- LLM provider configurations
    CREATE TABLE IF NOT EXISTS llm_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        provider TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key TEXT NOT NULL,
        model TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Webhook notification endpoints
    CREATE TABLE IF NOT EXISTS webhooks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'discord',
        url TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Briefing task definitions
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        template TEXT NOT NULL DEFAULT 'general',
        schedule TEXT NOT NULL DEFAULT 'manual',
        cron_expr TEXT,
        timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        categories_json TEXT NOT NULL DEFAULT '[]',
        time_range_hours INTEGER NOT NULL DEFAULT 24,
        llm_config_id INTEGER,
        webhook_ids_json TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Generated briefings history
    CREATE TABLE IF NOT EXISTS briefings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        template TEXT NOT NULL,
        content TEXT NOT NULL,
        content_html TEXT,
        article_count INTEGER NOT NULL DEFAULT 0,
        source_start TEXT,
        source_end TEXT,
        sent_to_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending',
        error_message TEXT,
        created_at TEXT NOT NULL
    );

    -- Custom/modified templates
    CREATE TABLE IF NOT EXISTS custom_templates (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        system_prompt TEXT NOT NULL,
        user_prompt_template TEXT NOT NULL,
        required_sections TEXT DEFAULT '[]',
        is_builtin INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Create indexes for common queries
    CREATE INDEX IF NOT EXISTS idx_briefings_created_at ON briefings(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_tasks_enabled ON tasks(enabled);
    """

    # Create tables first
    with _get_conn() as conn:
        conn.executescript(ddl)

    # Run migrations for existing databases (add missing columns)
    with _get_conn() as conn:
        # Migrate briefings table
        try:
            columns = conn.execute("PRAGMA table_info(briefings)").fetchall()
            column_names = [col[1] for col in columns]

            briefings_migrations = [
                ("sent_to_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("status", "TEXT NOT NULL DEFAULT 'success'"),
                ("error_message", "TEXT"),
            ]

            for col_name, col_def in briefings_migrations:
                if col_name not in column_names:
                    conn.execute(f"ALTER TABLE briefings ADD COLUMN {col_name} {col_def}")
                    logger.info(f"Added {col_name} column to briefings table")
        except sqlite3.OperationalError:
            pass

        # Migrate llm_configs table
        try:
            columns = conn.execute("PRAGMA table_info(llm_configs)").fetchall()
            column_names = [col[1] for col in columns]

            llm_migrations = [
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ]

            for col_name, col_def in llm_migrations:
                if col_name not in column_names:
                    conn.execute(f"ALTER TABLE llm_configs ADD COLUMN {col_name} {col_def}")
                    logger.info(f"Added {col_name} column to llm_configs table")
        except sqlite3.OperationalError:
            pass

        # Migrate webhooks table
        try:
            columns = conn.execute("PRAGMA table_info(webhooks)").fetchall()
            column_names = [col[1] for col in columns]

            webhook_migrations = [
                ("enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ]

            for col_name, col_def in webhook_migrations:
                if col_name not in column_names:
                    conn.execute(f"ALTER TABLE webhooks ADD COLUMN {col_name} {col_def}")
                    logger.info(f"Added {col_name} column to webhooks table")
        except sqlite3.OperationalError:
            pass

        # Migrate tasks table
        try:
            columns = conn.execute("PRAGMA table_info(tasks)").fetchall()
            column_names = [col[1] for col in columns]

            task_migrations = [
                ("cron_expr", "TEXT"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("last_run_at", "TEXT"),
            ]

            for col_name, col_def in task_migrations:
                if col_name not in column_names:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")
                    logger.info(f"Added {col_name} column to tasks table")
        except sqlite3.OperationalError:
            pass

        conn.commit()
    logger.info(f"Database initialized at {DB_PATH}")


def _get_setting(key: str, default: str | None = None) -> str | None:
    """Get a system setting from database."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else default


def _set_setting(key: str, value: str) -> None:
    """Set a system setting in database."""
    now = _utcnow_iso()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        conn.commit()


def _seed_settings_from_env() -> None:
    """Seed database settings from environment variables if not already set."""
    mapping = {
        "miniflux_url": os.getenv("MINIFLUX_URL", ""),
        "miniflux_token": os.getenv("MINIFLUX_TOKEN", ""),
    }
    for key, value in mapping.items():
        if value and not _get_setting(key):
            _set_setting(key, value)
            logger.debug(f"Seeded setting from env: {key}")


def _set_default_flag(table: str, current_id: int) -> None:
    """Set a record as the default, clearing previous defaults."""
    with _get_conn() as conn:
        conn.execute(f"UPDATE {table} SET is_default = 0")
        conn.execute(f"UPDATE {table} SET is_default = 1 WHERE id = ?", (current_id,))
        conn.commit()


def bootstrap() -> None:
    """Initialize application state (database, environment, etc.)."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _load_dotenv(ENV_PATH)
    _init_db()
    _seed_settings_from_env()
    _BOOTSTRAPPED = True
    logger.info("Application bootstrapped successfully")


# =============================================================================
# Pydantic Models
# =============================================================================

class LLMConfigBase(BaseModel):
    """Base model for LLM configuration."""
    name: str = Field(..., min_length=1, max_length=100, description="Configuration name")
    provider: str = Field(..., min_length=1, max_length=50, description="Provider ID")
    base_url: str = Field(..., min_length=1, description="API base URL")
    api_key: str = Field(..., min_length=1, description="API key")
    model: str = Field(..., min_length=1, description="Model name")
    is_default: bool = Field(default=False, description="Set as default configuration")


class LLMConfigCreate(LLMConfigBase):
    """Model for creating a new LLM configuration."""
    pass


class LLMConfigOut(BaseModel):
    """Model for LLM configuration output (API key masked for security)."""
    id: int
    name: str
    provider: str
    base_url: str
    model: str
    api_key_masked: str = Field(..., description="Masked API key (e.g., sk-***abc)")
    is_default: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class WebhookBase(BaseModel):
    """Base model for webhook configuration."""
    name: str = Field(..., min_length=1, max_length=100, description="Webhook name")
    type: str = Field(default="discord", description="Webhook type (discord, slack, etc.)")
    url: str = Field(..., min_length=1, description="Webhook URL")
    is_default: bool = Field(default=False, description="Set as default webhook")
    enabled: bool = Field(default=True, description="Enable or disable webhook")

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        supported = {"discord", "slack"}
        normalized = (v or "discord").strip().lower()
        if normalized not in supported:
            raise ValueError(
                f"Unsupported webhook type: {normalized}. Supported types: {sorted(supported)}"
            )
        return normalized

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")

        # For webhooks, we allow external URLs but still validate format
        # SSRF protection is applied at the point of sending
        is_valid, error = _validate_url_for_ssrf(v, allow_private=False)
        if not is_valid:
            # Log warning but don't block webhook URLs completely
            # The actual SSRF check happens when sending
            logger.warning("Webhook URL validation warning for %s: %s", v[:50], error)

        return v


class WebhookCreate(WebhookBase):
    """Model for creating a new webhook."""
    pass


class WebhookOut(BaseModel):
    """Model for webhook output (URL masked for security)."""
    id: int
    name: str
    type: str
    url_masked: str = Field(..., description="Masked webhook URL")
    is_default: bool
    enabled: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class TaskBase(BaseModel):
    """Base model for briefing task."""
    name: str = Field(..., min_length=1, max_length=100, description="Task name")
    template: str = Field(default="general", description="Briefing template ID")
    schedule: str = Field(default="manual", description="Schedule type (manual, cron)")
    cron_expr: str | None = Field(default=None, description="Cron expression for scheduled tasks")
    timezone: str = Field(default="Asia/Shanghai", description="Timezone for scheduling")
    categories: list[int] = Field(default_factory=list, description="Category IDs to include")
    time_range_hours: int = Field(default=24, ge=1, le=168, description="Hours to look back")
    llm_config_id: int | None = Field(default=None, description="LLM configuration ID")
    webhook_ids: list[int] = Field(default_factory=list, description="Webhook IDs for notifications")
    enabled: bool = Field(default=True, description="Enable or disable task")

    @field_validator("categories", "webhook_ids")
    @classmethod
    def validate_positive_id_list(
        cls,
        v: list[int],
    ) -> list[int]:
        """Validate that all IDs in the list are positive integers."""
        if any(item <= 0 for item in v):
            raise ValueError("All IDs must be positive integers")
        return v


class TaskCreate(TaskBase):
    """Model for creating a new task."""
    pass


class TaskUpdate(TaskBase):
    """Model for updating an existing task."""
    pass


class TaskOut(TaskBase):
    """Model for task output."""
    id: int
    last_run_at: str | None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class GenerateRequest(BaseModel):
    """Model for briefing generation request."""
    task_id: int | None = Field(default=None, description="Task ID to use as template")
    template: str | None = Field(default=None, description="Briefing template ID")
    category_ids: list[int] | None = Field(default=None, description="Category IDs to include")
    time_range_hours: int | None = Field(default=None, ge=1, le=168, description="Hours to look back")
    llm_config_id: int | None = Field(default=None, description="LLM configuration ID")
    webhook_ids: list[int] | None = Field(default=None, description="Webhook IDs for notifications")

    @field_validator("category_ids", "webhook_ids")
    @classmethod
    def validate_positive_id_list(
        cls,
        v: list[int] | None,
    ) -> list[int] | None:
        """Validate that all IDs in the list are positive integers."""
        if v is None:
            return v
        if any(item <= 0 for item in v):
            raise ValueError("All IDs must be positive integers")
        return v


class BriefingOut(BaseModel):
    """Model for briefing output."""
    id: int
    task_id: int | None
    template: str
    content: str
    article_count: int
    source_start: str | None
    source_end: str | None
    sent_to: list[dict[str, Any]]
    status: str
    error_message: str | None
    created_at: str

    class Config:
        from_attributes = True


class TemplateOut(BaseModel):
    """Model for template output."""
    id: str
    name: str
    description: str
    system_prompt: str
    user_prompt_template: str
    required_sections: list[str]
    is_builtin: bool = True
    is_modified: bool = False


class TemplateUpdate(BaseModel):
    """Model for template update."""
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    user_prompt_template: str | None = None
    required_sections: list[str] | None = None


class TemplateCreate(BaseModel):
    """Model for creating a custom template."""
    id: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    system_prompt: str = Field(..., min_length=1)
    user_prompt_template: str = Field(..., min_length=1)
    required_sections: list[str] = Field(default_factory=list)


class LLMTestRequest(BaseModel):
    """Model for LLM connection test."""
    llm_config_id: int | None = Field(default=None, description="LLM configuration ID to test")
    base_url: str | None = Field(default=None, description="Override base URL")
    api_key: str | None = Field(default=None, description="Override API key")
    model: str | None = Field(default=None, description="Override model name")


class WebhookTestRequest(BaseModel):
    """Model for webhook test."""
    webhook_id: int | None = Field(default=None, description="Webhook ID to test")
    url: str | None = Field(default=None, description="Override webhook URL")
    type: str | None = Field(default=None, description="Override webhook type (discord, slack)")
    content: str = Field(default="OpusSummary webhook test message", description="Test message content")

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str | None) -> str | None:
        """Validate webhook type if provided."""
        if v is None:
            return v
        supported = {"discord", "slack"}
        normalized = v.strip().lower()
        if normalized not in supported:
            raise ValueError(
                f"Unsupported webhook type: {normalized}. Supported types: {sorted(supported)}"
            )
        return normalized


class MinifluxTestRequest(BaseModel):
    """Model for Miniflux connection test."""
    url: str | None = Field(default=None, description="Override Miniflux URL")
    token: str | None = Field(default=None, description="Override API token")


class SystemSettings(BaseModel):
    """Model for system settings."""
    miniflux_url: str | None = None
    miniflux_token: str | None = None


# =============================================================================
# Database Row to Model Converters
# =============================================================================

def _mask_api_key(api_key: str) -> str:
    """Mask API key for display, showing only first 4 and last 3 characters."""
    if not api_key or len(api_key) < 10:
        return "***"
    return f"{api_key[:4]}***{api_key[-3:]}"


def _llm_row_to_model(row: sqlite3.Row) -> LLMConfigOut:
    """Convert database row to LLMConfigOut model."""
    return LLMConfigOut(
        id=row["id"],
        name=row["name"],
        provider=row["provider"],
        base_url=row["base_url"],
        model=row["model"],
        api_key_masked=_mask_api_key(row["api_key"]),
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _mask_url(url: str) -> str:
    """Mask URL for display, showing only domain and path hints."""
    if not url:
        return "***"
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        # Show scheme and domain, mask the rest
        domain = parsed.netloc
        if len(domain) > 20:
            domain = domain[:17] + "..."
        return f"{parsed.scheme}://{domain}/**"
    except Exception:
        return "***"


def _webhook_row_to_model(row: sqlite3.Row) -> WebhookOut:
    """Convert database row to WebhookOut model."""
    return WebhookOut(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        url_masked=_mask_url(row["url"]),
        is_default=bool(row["is_default"]),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _task_row_to_model(row: sqlite3.Row) -> TaskOut:
    """Convert database row to TaskOut model."""
    return TaskOut(
        id=row["id"],
        name=row["name"],
        template=row["template"],
        schedule=row["schedule"],
        cron_expr=row["cron_expr"],
        timezone=row["timezone"],
        categories=_json_loads(row["categories_json"], []),
        time_range_hours=row["time_range_hours"],
        llm_config_id=row["llm_config_id"],
        webhook_ids=_json_loads(row["webhook_ids_json"], []),
        enabled=bool(row["enabled"]),
        last_run_at=row["last_run_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _briefing_row_to_model(row: sqlite3.Row) -> BriefingOut:
    """Convert database row to BriefingOut model."""
    return BriefingOut(
        id=row["id"],
        task_id=row["task_id"],
        template=row["template"],
        content=row["content"],
        article_count=row["article_count"],
        source_start=row["source_start"],
        source_end=row["source_end"],
        sent_to=_json_loads(row["sent_to_json"], []),
        status=row["status"],
        error_message=row["error_message"],
        created_at=row["created_at"],
    )


# =============================================================================
# Service Functions
# =============================================================================

def _resolve_miniflux_config(
    override_url: str | None = None,
    override_token: str | None = None,
) -> tuple[str, str]:
    """Resolve Miniflux connection configuration."""
    url = (override_url or _get_setting("miniflux_url") or os.getenv("MINIFLUX_URL", "")).strip()
    token = (override_token or _get_setting("miniflux_token") or os.getenv("MINIFLUX_TOKEN", "")).strip()
    if not url or not token:
        raise HTTPException(
            status_code=400,
            detail="Miniflux configuration missing. Please set MINIFLUX_URL and MINIFLUX_TOKEN in .env or test connection via API.",
        )
    return url, token


def _collect_entries(
    url: str,
    token: str,
    category_ids: list[int],
    time_range_hours: int,
) -> list[dict[str, Any]]:
    """Collect entries from Miniflux based on filters."""
    client = MinifluxClient(base_url=url, api_token=token)
    try:
        entries = client.get_unread_entries(limit=500)
    finally:
        client.close()

    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=time_range_hours)
    results: list[dict[str, Any]] = []

    for entry in entries:
        # Filter by category
        category = ((entry.get("feed") or {}).get("category") or {})
        category_id = category.get("id")
        if category_ids and category_id not in category_ids:
            continue

        # Filter by time
        published_at = _parse_iso(entry.get("published_at"))
        changed_at = _parse_iso(entry.get("changed_at"))
        timestamp = published_at or changed_at
        if timestamp and timestamp < threshold:
            continue

        results.append(entry)

    logger.info(f"Collected {len(results)} entries matching filters")
    return results


def _build_briefing_markdown(template: str, entries: list[dict[str, Any]]) -> str:
    """Build briefing markdown content from entries."""
    template_info = BRIEFING_TEMPLATES.get(template, BRIEFING_TEMPLATES["general"])

    lines = [
        f"# OpusBrief - {template_info['name']}",
        "",
        f"**生成时间**: {_utcnow_iso()}",
        f"**文章总数**: {len(entries)}",
        "",
        "---",
        "",
        "## 文章列表",
        "",
    ]

    if not entries:
        lines.append("暂无符合条件的文章。")
    else:
        for i, entry in enumerate(entries[:50], 1):  # Limit to 50 entries
            feed = entry.get("feed") or {}
            feed_title = feed.get("title", "Unknown Feed")
            title = entry.get("title", "无标题")
            url = entry.get("url", "")
            published = entry.get("published_at", "")

            lines.append(f"### {i}. {title}")
            lines.append(f"- **来源**: {feed_title}")
            if url:
                lines.append(f"- **链接**: {url}")
            if published:
                lines.append(f"- **发布时间**: {published}")
            lines.append("")

    lines.extend([
        "---",
        "",
        "## 说明",
        "",
        f"当前使用模板: **{template_info['name']}** - {template_info['description']}",
        "",
        "_此简报由 OpusBrief 自动生成_",
    ])

    return "\n".join(lines)


def _resolve_llm_config(
    llm_config_id: int | None,
    *,
    strict: bool = False,
) -> sqlite3.Row | None:
    """
    Resolve LLM configuration from database.

    Priority:
    1. Explicit llm_config_id (if provided)
    2. Default config (is_default = 1)
    3. First available config as fallback

    Args:
        llm_config_id: Optional explicit config ID
        strict: If True, raise HTTPException when explicit ID not found.
                If False (default), log warning and return None for fallback.

    Returns:
        Database row with LLM config, or None if no config exists

    Raises:
        HTTPException: If strict=True and explicit llm_config_id is not found
    """
    with _get_conn() as conn:
        # If explicit ID provided, look it up
        if llm_config_id is not None:
            row = conn.execute(
                """
                SELECT id, provider, base_url, api_key, model
                FROM llm_configs
                WHERE id = ?
                """,
                (llm_config_id,),
            ).fetchone()
            if row is None:
                if strict:
                    raise HTTPException(
                        status_code=404,
                        detail=f"LLM config not found: {llm_config_id}"
                    )
                # Non-strict mode: log warning and try fallback
                logger.warning(
                    "LLM config %d not found, falling back to default",
                    llm_config_id
                )
            else:
                return row

        # Try to get default config
        row = conn.execute(
            """
            SELECT id, provider, base_url, api_key, model
            FROM llm_configs
            WHERE is_default = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if row is not None:
            return row

        # Fallback: get first available config
        return conn.execute(
            """
            SELECT id, provider, base_url, api_key, model
            FROM llm_configs
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()


def _normalize_briefing_template(template: str) -> BriefingTemplate:
    """
    Normalize template string to BriefingTemplate enum.

    Args:
        template: Template identifier string

    Returns:
        BriefingTemplate enum value, defaults to GENERAL if invalid
    """
    try:
        return BriefingTemplate(template)
    except ValueError:
        logger.warning(
            "Unknown briefing template '%s', falling back to 'general'",
            template
        )
        return BriefingTemplate.GENERAL


def _prepare_articles_for_briefing(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    Convert Miniflux entries to BriefingGenerator-compatible article format.

    BriefingGenerator requires these fields:
    - title: Article title
    - url: Article URL
    - published_at: Publication timestamp
    - source: Feed/source name
    - content: Article content (falls back to summary/description)
    - category: Category label

    Args:
        entries: Raw entries from Miniflux

    Returns:
        List of normalized article dictionaries
    """
    articles: list[dict[str, str]] = []

    for entry in entries:
        feed = entry.get("feed") or {}
        category = feed.get("category") or {}

        # Content priority: content > summary > description
        content = (
            entry.get("content")
            or entry.get("summary")
            or entry.get("description")
            or ""
        )

        # Build article dict with all required fields
        article = {
            "title": str(entry.get("title") or "无标题").strip(),
            "url": str(entry.get("url") or "").strip(),
            "published_at": str(
                entry.get("published_at")
                or entry.get("changed_at")
                or ""
            ).strip(),
            "source": str(feed.get("title") or "Unknown Feed").strip(),
            "content": str(content).strip(),
            "category": str(
                category.get("title")
                or category.get("id")
                or ""
            ).strip(),
        }
        articles.append(article)

    return articles


def _resolve_webhook_targets(webhook_ids: list[int]) -> list[tuple[int, str, str]]:
    """Resolve webhook IDs to (id, type, url)."""
    if not webhook_ids:
        return []
    placeholders = ",".join("?" for _ in webhook_ids)
    query = f"SELECT id, type, url FROM webhooks WHERE id IN ({placeholders}) AND enabled = 1"
    with _get_conn() as conn:
        rows = conn.execute(query, tuple(webhook_ids)).fetchall()
    return [(row["id"], row["type"], row["url"]) for row in rows]


def _send_webhook(webhook_type: str, url: str, content: str) -> tuple[bool, int, str]:
    """
    Send content to webhook URL according to webhook type.

    This method properly handles:
    - Discord's 2000 character limit with automatic message chunking
    - Rate limiting with retry logic
    - Connection errors with proper error handling
    - Multiple webhook types (discord, slack)

    Args:
        webhook_type: Webhook type (discord, slack)
        url: Webhook URL
        content: Content to send (will be automatically chunked if too long)

    Returns:
        Tuple of (success, status_code, detail_message)
    """
    webhook_type = (webhook_type or "discord").strip().lower()

    if webhook_type == "discord":
        try:
            with DiscordSender(url, timeout=15.0, max_retries=3) as sender:
                success_count = sender.send_briefing(content, title="OpusBrief")

                if success_count > 0:
                    return True, 200, f"Sent {success_count} message(s) successfully"
                else:
                    return False, 0, "Failed to send any messages"

        except DiscordSenderError as exc:
            logger.error("Discord webhook send failed: %s", exc)
            return False, 0, str(exc)
        except Exception as exc:
            logger.error("Discord webhook send failed with unexpected error: %s", exc)
            return False, 0, str(exc)

    if webhook_type == "slack":
        try:
            resp = httpx.post(url, json={"text": content}, timeout=15.0)
            if 200 <= resp.status_code < 300:
                return True, resp.status_code, "Slack message sent successfully"
            return False, resp.status_code, (resp.text[:300] if resp.text else "Slack webhook failed")
        except httpx.HTTPError as exc:
            logger.error("Slack webhook send failed: %s", exc)
            return False, 0, str(exc)

    return False, 400, f"Unsupported webhook type: {webhook_type}"


def generate_briefing(payload: GenerateRequest) -> dict[str, Any]:
    """
    Generate a briefing based on request parameters.

    This function:
    1. Collects articles from Miniflux based on filters
    2. Attempts to generate a structured briefing using LLM
    3. Falls back to simple list format if LLM fails
    4. Saves the briefing to database
    5. Optionally sends to configured webhooks

    Args:
        payload: Generation request parameters

    Returns:
        Dictionary with briefing metadata and content

    Raises:
        HTTPException: If task not found or explicit LLM config not found
    """
    # Token limit constants (prevent LLM context overflow)
    MAX_LLM_ARTICLES = 25  # Reduced from 50 to prevent token overflow
    MAX_ARTICLE_CONTENT_CHARS = 2400  # Max characters per article content
    MAX_TOTAL_ARTICLE_CHARS = 40000  # Max total characters for all articles combined

    # Resolve task if specified
    task_row: sqlite3.Row | None = None
    if payload.task_id is not None:
        with _get_conn() as conn:
            task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (payload.task_id,)).fetchone()
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {payload.task_id}")

    # Merge parameters from task and request
    raw_template = payload.template or (task_row["template"] if task_row else "general")
    resolved_template = _normalize_briefing_template(raw_template)

    category_ids = payload.category_ids
    if category_ids is None:
        category_ids = _json_loads(task_row["categories_json"], []) if task_row else []
    time_range_hours = payload.time_range_hours or (task_row["time_range_hours"] if task_row else 24)
    webhook_ids = payload.webhook_ids
    if webhook_ids is None:
        webhook_ids = _json_loads(task_row["webhook_ids_json"], []) if task_row else []

    # Resolve LLM config (priority: payload > task > default)
    # Use strict mode for payload (user explicitly specified), non-strict for task
    llm_config_id = payload.llm_config_id
    is_explicit_llm_config = payload.llm_config_id is not None
    if llm_config_id is None and task_row is not None:
        llm_config_id = task_row["llm_config_id"]

    # Validate explicit LLM config early (before checking entries)
    # This ensures consistent behavior regardless of article count
    if is_explicit_llm_config:
        _resolve_llm_config(llm_config_id, strict=True)

    # Collect entries from Miniflux
    url, token = _resolve_miniflux_config()
    entries = _collect_entries(url, token, category_ids, time_range_hours)

    # Prepare fallback content (simple list format)
    fallback_content = _build_briefing_markdown(raw_template, entries)
    content = fallback_content
    error_message: str | None = None
    used_llm = False

    # Determine status based on generation result
    # - success: LLM generated content
    # - degraded: fallback to list format (LLM failed or no config)
    # - success: no articles (empty list format is expected)
    status = "success"

    # Attempt LLM-based briefing generation
    if not entries:
        logger.info("No entries found, using empty list format")
    else:
        # Resolve LLM config (non-strict for task/default path, already validated if explicit)
        llm_row = _resolve_llm_config(llm_config_id, strict=False)
        if llm_row is None:
            error_message = "No LLM configuration available, using simple list format"
            status = "degraded"
            logger.warning(error_message)
        else:
            llm_client: LLMClient | None = None
            try:
                # Determine API style based on provider
                provider = str(llm_row["provider"] or "").strip().lower()
                api_style = "anthropic" if provider == "anthropic" else "openai"

                # Initialize LLM client
                llm_client = LLMClient(
                    base_url=str(llm_row["base_url"]),
                    api_key=str(llm_row["api_key"]),
                    model=str(llm_row["model"]),
                    provider_id=provider or "openai",
                    api_style=api_style,
                )

                # Get timezone from task or use default
                task_timezone = "Asia/Shanghai"
                if task_row is not None and task_row["timezone"]:
                    task_timezone = str(task_row["timezone"])

                # Prepare articles for briefing generator
                # Limit to prevent token overflow, sorted by recency (already done in _collect_entries)
                limited_entries = entries[:MAX_LLM_ARTICLES]
                articles = _prepare_articles_for_briefing(limited_entries)

                # Truncate article content to prevent token overflow
                total_chars = 0
                truncated_articles: list[dict[str, str]] = []
                ellipsis = "..."
                for article in articles:
                    content = article.get("content", "")
                    # Truncate individual article content if too long
                    if len(content) > MAX_ARTICLE_CONTENT_CHARS:
                        keep = MAX_ARTICLE_CONTENT_CHARS - len(ellipsis)
                        if keep > 0:
                            content = content[:keep] + ellipsis
                        else:
                            content = content[:MAX_ARTICLE_CONTENT_CHARS]
                        article = {**article, "content": content}

                    # Check if adding this article would exceed total budget
                    if total_chars + len(content) > MAX_TOTAL_ARTICLE_CHARS:
                        # Truncate content to fit remaining budget
                        remaining = MAX_TOTAL_ARTICLE_CHARS - total_chars
                        if remaining > 0:
                            if len(content) > remaining:
                                keep = remaining - len(ellipsis)
                                if keep > 0:
                                    content = content[:keep] + ellipsis
                                else:
                                    content = content[:remaining]
                            article = {**article, "content": content}
                            truncated_articles.append(article)
                            total_chars += len(content)
                        break

                    truncated_articles.append(article)
                    total_chars += len(content)

                articles = truncated_articles
                logger.info(
                    "Prepared %d articles for LLM (total chars: %d)",
                    len(articles),
                    total_chars,
                )

                # Create briefing generator and request
                generator = BriefingGenerator(llm_client=llm_client)
                request = BriefingGenerateRequest(
                    template=resolved_template,
                    articles=articles,
                    timezone=task_timezone,
                    time_range_hours=time_range_hours,
                    output_format="markdown",
                    article_content_max_length=MAX_ARTICLE_CONTENT_CHARS,  # Pass limit to generator
                )

                # Generate briefing
                result = generator.generate(request)

                # Validate result
                if result.markdown and result.markdown.strip():
                    content = result.markdown
                    used_llm = True
                    logger.info(
                        "LLM briefing generated successfully: template=%s, articles=%d (of %d)",
                        resolved_template.value,
                        len(articles),
                        len(entries),
                    )
                else:
                    error_message = "LLM returned empty content, using simple list format"
                    status = "degraded"
                    logger.warning(error_message)

            except Exception as exc:
                error_message = f"LLM briefing generation failed: {exc}"
                status = "degraded"
                logger.exception(error_message)
                content = fallback_content
            finally:
                # Ensure client is properly closed
                if llm_client is not None:
                    try:
                        llm_client.close()
                    except Exception as close_exc:
                        logger.warning("Failed to close LLM client: %s", close_exc)

    # Calculate time range
    now = datetime.now(timezone.utc)
    source_start = (now - timedelta(hours=time_range_hours)).isoformat()
    source_end = now.isoformat()

    # Send to webhooks
    sent_to: list[dict[str, Any]] = []
    for webhook_id, webhook_type, webhook_url in _resolve_webhook_targets(webhook_ids):
        ok, status_code, detail = _send_webhook(webhook_type, webhook_url, content)
        sent_to.append({
            "webhook_id": webhook_id,
            "webhook_type": webhook_type,
            "success": ok,
            "status_code": status_code,
            "detail": detail,
        })

    # Save to database
    # Use resolved template value for consistency
    created_at = _utcnow_iso()
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO briefings (
                task_id, template, content, article_count,
                source_start, source_end, sent_to_json, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.task_id,
                resolved_template.value,  # Use normalized template
                content,
                len(entries),
                source_start,
                source_end,
                _json_dumps(sent_to),
                status,
                error_message,
                created_at,
            ),
        )
        conn.commit()
        briefing_id = int(cursor.lastrowid)

    logger.info(
        "Generated briefing %d: articles=%d, used_llm=%s, status=%s",
        briefing_id,
        len(entries),
        used_llm,
        status,
    )

    return {
        "id": briefing_id,
        "task_id": payload.task_id,
        "template": resolved_template.value,
        "article_count": len(entries),
        "content": content,
        "source_start": source_start,
        "source_end": source_end,
        "sent_to": sent_to,
        "created_at": created_at,
        "status": status,
        "error_message": error_message,
    }


def test_llm_connection(payload: LLMTestRequest) -> dict[str, Any]:
    """Test LLM connection."""
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = payload.model

    # Resolve configuration
    if payload.llm_config_id is not None:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT base_url, api_key, model FROM llm_configs WHERE id = ?",
                (payload.llm_config_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"LLM config not found: {payload.llm_config_id}")
        base_url = row["base_url"]
        api_key = row["api_key"]
        model = model or row["model"]
    else:
        base_url = payload.base_url
        api_key = payload.api_key

    if not base_url or not api_key:
        raise HTTPException(status_code=400, detail="Either llm_config_id or base_url + api_key required")

    # Test connection
    headers = {"Authorization": f"Bearer {api_key}"}
    models_url = f"{base_url.rstrip('/')}/models"

    try:
        # Try /models endpoint first
        resp = httpx.get(models_url, headers=headers, timeout=15.0)
        if 200 <= resp.status_code < 300:
            return {"success": True, "status_code": resp.status_code, "detail": "Models endpoint OK"}

        # If models fails, try a simple chat completion
        if model:
            chat_url = f"{base_url.rstrip('/')}/chat/completions"
            probe = httpx.post(
                chat_url,
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                timeout=20.0,
            )
            ok = 200 <= probe.status_code < 300
            return {
                "success": ok,
                "status_code": probe.status_code,
                "detail": probe.text[:300],
            }

        return {"success": False, "status_code": resp.status_code, "detail": resp.text[:300]}
    except httpx.HTTPError as exc:
        return {"success": False, "status_code": 0, "detail": str(exc)}


def test_miniflux_connection(payload: MinifluxTestRequest) -> dict[str, Any]:
    """Test Miniflux connection."""
    url, token = _resolve_miniflux_config(
        override_url=payload.url,
        override_token=payload.token,
    )

    client = MinifluxClient(base_url=url, api_token=token)
    try:
        ok = client.test_connection()
    finally:
        client.close()

    # Save configuration if test succeeds
    if ok:
        _set_setting("miniflux_url", url)
        _set_setting("miniflux_token", token)
        logger.info(f"Miniflux configuration saved: {url}")

    return {
        "success": ok,
        "url": url,
        "saved_to_db": ok,
    }


# =============================================================================
# FastAPI Application
# =============================================================================

from contextlib import asynccontextmanager

# Global scheduler instance
_app_scheduler: BriefingScheduler | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global _app_scheduler
    logger.info("Initializing BriefingScheduler...")
    _app_scheduler = BriefingScheduler()
    _app_scheduler.start()
    yield
    # Shutdown
    if _app_scheduler:
        logger.info("Shutting down BriefingScheduler...")
        _app_scheduler.shutdown(wait=False)

def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    bootstrap()

    app = FastAPI(
        title="OpusBrief Web Admin",
        version="0.1.0",
        description="Web administration interface for OpusBrief RSS briefing system",
        lifespan=lifespan,
    )

    # CORS middleware - restricted to localhost for security
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://127.0.0.1",
            "http://[::1]",
        ],
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Mount static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # =========================================================================
    # Frontend Routes
    # =========================================================================

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Serve frontend index page."""
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Frontend not found: web/static/index.html")
        return FileResponse(index_path)

    # =========================================================================
    # System Settings API
    # =========================================================================

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        """Get system settings."""
        return {
            "miniflux_url": _get_setting("miniflux_url") or "",
            "miniflux_configured": bool(_get_setting("miniflux_url") and _get_setting("miniflux_token")),
            "providers": LLM_PROVIDERS,
            "templates": BRIEFING_TEMPLATES,
        }

    # =========================================================================
    # LLM Configuration API
    # =========================================================================

    @app.get("/api/llm-configs", response_model=list[LLMConfigOut])
    def list_llm_configs() -> list[LLMConfigOut]:
        """List all LLM configurations."""
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_configs ORDER BY is_default DESC, id DESC"
            ).fetchall()
        return [_llm_row_to_model(row) for row in rows]

    @app.post("/api/llm-configs", response_model=LLMConfigOut, status_code=201)
    def create_llm_config(payload: LLMConfigCreate) -> LLMConfigOut:
        """Create a new LLM configuration."""
        now = _utcnow_iso()
        with _get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO llm_configs (
                    name, provider, base_url, api_key, model,
                    is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.name,
                    payload.provider,
                    payload.base_url,
                    payload.api_key,
                    payload.model,
                    int(payload.is_default),
                    now,
                    now,
                ),
            )
            conn.commit()
            new_id = int(cursor.lastrowid)

        if payload.is_default:
            _set_default_flag("llm_configs", new_id)

        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM llm_configs WHERE id = ?", (new_id,)).fetchone()
        return _llm_row_to_model(row)

    @app.delete("/api/llm-configs/{config_id}")
    def delete_llm_config(config_id: int) -> dict[str, Any]:
        """Delete an LLM configuration."""
        with _get_conn() as conn:
            exists = conn.execute("SELECT 1 FROM llm_configs WHERE id = ?", (config_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail=f"LLM config not found: {config_id}")
            conn.execute("DELETE FROM llm_configs WHERE id = ?", (config_id,))
            conn.commit()
        return {"ok": True, "id": config_id}

    @app.put("/api/llm-configs/{config_id}", response_model=LLMConfigOut)
    def update_llm_config(config_id: int, payload: LLMConfigCreate) -> LLMConfigOut:
        """Update an existing LLM configuration."""
        now = _utcnow_iso()
        with _get_conn() as conn:
            exists = conn.execute("SELECT 1 FROM llm_configs WHERE id = ?", (config_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail=f"LLM config not found: {config_id}")
            
            # 如果设置为默认，先清除其他默认标记
            if payload.is_default:
                conn.execute("UPDATE llm_configs SET is_default = 0 WHERE is_default = 1")

            conn.execute(
                """
                UPDATE llm_configs
                SET
                    name = ?, provider = ?, base_url = ?, api_key = ?, model = ?,
                    is_default = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.name,
                    payload.provider,
                    payload.base_url,
                    payload.api_key,
                    payload.model,
                    int(payload.is_default),
                    now,
                    config_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM llm_configs WHERE id = ?", (config_id,)).fetchone()
        return _llm_row_to_model(row)

    @app.put("/api/llm-configs/{config_id}/default")
    def set_default_llm_config(config_id: int) -> dict[str, Any]:
        """将指定 LLM 配置设为默认。"""
        with _get_conn() as conn:
            exists = conn.execute("SELECT 1 FROM llm_configs WHERE id = ?", (config_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail=f"LLM config not found: {config_id}")
        _set_default_flag("llm_configs", config_id)
        return {"ok": True, "id": config_id}

    # =========================================================================
    # Webhook API
    # =========================================================================

    @app.get("/api/webhooks", response_model=list[WebhookOut])
    def list_webhooks() -> list[WebhookOut]:
        """List all webhooks."""
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM webhooks ORDER BY is_default DESC, id DESC"
            ).fetchall()
        return [_webhook_row_to_model(row) for row in rows]

    @app.post("/api/webhooks", response_model=WebhookOut, status_code=201)
    def create_webhook(payload: WebhookCreate) -> WebhookOut:
        """Create a new webhook."""
        now = _utcnow_iso()
        with _get_conn() as conn:
            # 如果设置为默认，先清除其他默认标记
            if payload.is_default:
                conn.execute("UPDATE webhooks SET is_default = 0 WHERE is_default = 1")

            cursor = conn.execute(
                """
                INSERT INTO webhooks (name, type, url, is_default, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.name,
                    payload.type,
                    payload.url,
                    int(payload.is_default),
                    int(payload.enabled),
                    now,
                    now,
                ),
            )
            conn.commit()
            new_id = int(cursor.lastrowid)

        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (new_id,)).fetchone()
        return _webhook_row_to_model(row)

    @app.delete("/api/webhooks/{webhook_id}")
    def delete_webhook(webhook_id: int) -> dict[str, Any]:
        """Delete a webhook."""
        with _get_conn() as conn:
            exists = conn.execute("SELECT 1 FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail=f"Webhook not found: {webhook_id}")
            conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
            conn.commit()
        return {"ok": True, "id": webhook_id}

    @app.put("/api/webhooks/{webhook_id}", response_model=WebhookOut)
    def update_webhook(webhook_id: int, payload: WebhookCreate) -> WebhookOut:
        """Update an existing webhook."""
        now = _utcnow_iso()
        with _get_conn() as conn:
            exists = conn.execute("SELECT 1 FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail=f"Webhook not found: {webhook_id}")
            
            # 如果设置为默认，先清除其他默认标记
            if payload.is_default:
                conn.execute("UPDATE webhooks SET is_default = 0 WHERE is_default = 1")

            conn.execute(
                """
                UPDATE webhooks
                SET name = ?, type = ?, url = ?, is_default = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.name,
                    payload.type,
                    payload.url,
                    int(payload.is_default),
                    int(payload.enabled),
                    now,
                    webhook_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        return _webhook_row_to_model(row)

    # =========================================================================
    # Task API
    # =========================================================================

    @app.get("/api/tasks", response_model=list[TaskOut])
    def list_tasks() -> list[TaskOut]:
        """List all briefing tasks."""
        with _get_conn() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
        return [_task_row_to_model(row) for row in rows]

    @app.post("/api/tasks", response_model=TaskOut, status_code=201)
    def create_task(payload: TaskCreate) -> TaskOut:
        """Create a new briefing task."""
        now = _utcnow_iso()
        with _get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    name, template, schedule, cron_expr, timezone, categories_json,
                    time_range_hours, llm_config_id, webhook_ids_json, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.name,
                    payload.template,
                    payload.schedule,
                    payload.cron_expr,
                    payload.timezone,
                    _json_dumps(payload.categories),
                    payload.time_range_hours,
                    payload.llm_config_id,
                    _json_dumps(payload.webhook_ids),
                    int(payload.enabled),
                    now,
                    now,
                ),
            )
            conn.commit()
            new_id = int(cursor.lastrowid)

        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone()
            
        if _app_scheduler:
            _app_scheduler.reload()
            
        return _task_row_to_model(row)

    @app.put("/api/tasks/{task_id}", response_model=TaskOut)
    def update_task(task_id: int, payload: TaskUpdate) -> TaskOut:
        """Update an existing task."""
        now = _utcnow_iso()
        with _get_conn() as conn:
            exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
            conn.execute(
                """
                UPDATE tasks
                SET
                    name = ?, template = ?, schedule = ?, cron_expr = ?,
                    timezone = ?, categories_json = ?, time_range_hours = ?,
                    llm_config_id = ?, webhook_ids_json = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.name,
                    payload.template,
                    payload.schedule,
                    payload.cron_expr,
                    payload.timezone,
                    _json_dumps(payload.categories),
                    payload.time_range_hours,
                    payload.llm_config_id,
                    _json_dumps(payload.webhook_ids),
                    int(payload.enabled),
                    now,
                    task_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            
        if _app_scheduler:
            _app_scheduler.reload()
            
        return _task_row_to_model(row)

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int) -> dict[str, Any]:
        """Delete a task."""
        with _get_conn() as conn:
            exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
            
        if _app_scheduler:
            _app_scheduler.reload()
            
        return {"ok": True, "id": task_id}

    # =========================================================================
    # Generate API
    # =========================================================================

    @app.post("/api/generate")
    def generate(payload: GenerateRequest) -> dict[str, Any]:
        """Generate a briefing manually."""
        return generate_briefing(payload)

    # =========================================================================
    # Briefing History API
    # =========================================================================

    @app.get("/api/briefings", response_model=list[BriefingOut])
    def list_briefings(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        task_id: int | None = Query(default=None),
    ) -> list[BriefingOut]:
        """List briefing history."""
        with _get_conn() as conn:
            if task_id:
                rows = conn.execute(
                    """
                    SELECT * FROM briefings
                    WHERE task_id = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (task_id, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM briefings
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        return [_briefing_row_to_model(row) for row in rows]

    @app.get("/api/briefings/{briefing_id}", response_model=BriefingOut)
    def get_briefing(briefing_id: int) -> BriefingOut:
        """Get a specific briefing."""
        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM briefings WHERE id = ?", (briefing_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Briefing not found: {briefing_id}")
        return _briefing_row_to_model(row)

    # =========================================================================
    # Categories API
    # =========================================================================

    @app.get("/api/categories")
    def list_categories() -> list[dict[str, Any]]:
        """List Miniflux categories."""
        url, token = _resolve_miniflux_config()
        client = MinifluxClient(base_url=url, api_token=token)
        try:
            return client.get_categories()
        finally:
            client.close()

    # =========================================================================
    # Templates API
    # =========================================================================

    @app.get("/api/templates", response_model=list[TemplateOut])
    def list_templates() -> list[TemplateOut]:
        """List all available templates (built-in + custom)."""
        templates: list[TemplateOut] = []

        # Get built-in templates
        for prompt_template in list_prompt_templates():
            # Check if this template has been customized
            with _get_conn() as conn:
                custom_row = conn.execute(
                    "SELECT * FROM custom_templates WHERE id = ?",
                    (prompt_template.template_id.value,)
                ).fetchone()

            if custom_row:
                # Return customized version
                templates.append(TemplateOut(
                    id=prompt_template.template_id.value,
                    name=custom_row["name"] or prompt_template.name,
                    description=custom_row["description"] or prompt_template.description,
                    system_prompt=custom_row["system_prompt"] or prompt_template.system_prompt,
                    user_prompt_template=custom_row["user_prompt_template"] or prompt_template.user_prompt_template,
                    required_sections=json.loads(custom_row["required_sections"] or "[]") if custom_row["required_sections"] else list(prompt_template.required_sections),
                    is_builtin=True,
                    is_modified=True,
                ))
            else:
                # Return original built-in template
                templates.append(TemplateOut(
                    id=prompt_template.template_id.value,
                    name=prompt_template.name,
                    description=prompt_template.description,
                    system_prompt=prompt_template.system_prompt,
                    user_prompt_template=prompt_template.user_prompt_template,
                    required_sections=list(prompt_template.required_sections),
                    is_builtin=True,
                    is_modified=False,
                ))

        # Get custom templates
        with _get_conn() as conn:
            custom_rows = conn.execute(
                "SELECT * FROM custom_templates WHERE is_builtin = 0 OR is_builtin IS NULL"
            ).fetchall()

        for row in custom_rows:
            # Skip if already added as modified built-in
            if any(t.id == row["id"] and t.is_builtin for t in templates):
                continue
            templates.append(TemplateOut(
                id=row["id"],
                name=row["name"],
                description=row["description"] or "",
                system_prompt=row["system_prompt"],
                user_prompt_template=row["user_prompt_template"],
                required_sections=json.loads(row["required_sections"] or "[]"),
                is_builtin=False,
                is_modified=False,
            ))

        return templates

    @app.get("/api/templates/{template_id}", response_model=TemplateOut)
    def get_template(template_id: str) -> TemplateOut:
        """Get a specific template by ID."""
        # First check if it's a built-in template
        try:
            prompt_template = get_prompt_template(template_id)
            # Check for customization
            with _get_conn() as conn:
                custom_row = conn.execute(
                    "SELECT * FROM custom_templates WHERE id = ?",
                    (template_id,)
                ).fetchone()

            if custom_row:
                return TemplateOut(
                    id=template_id,
                    name=custom_row["name"] or prompt_template.name,
                    description=custom_row["description"] or prompt_template.description,
                    system_prompt=custom_row["system_prompt"] or prompt_template.system_prompt,
                    user_prompt_template=custom_row["user_prompt_template"] or prompt_template.user_prompt_template,
                    required_sections=json.loads(custom_row["required_sections"] or "[]") if custom_row["required_sections"] else list(prompt_template.required_sections),
                    is_builtin=True,
                    is_modified=True,
                )

            return TemplateOut(
                id=template_id,
                name=prompt_template.name,
                description=prompt_template.description,
                system_prompt=prompt_template.system_prompt,
                user_prompt_template=prompt_template.user_prompt_template,
                required_sections=list(prompt_template.required_sections),
                is_builtin=True,
                is_modified=False,
            )
        except KeyError:
            pass

        # Check custom templates
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM custom_templates WHERE id = ?",
                (template_id,)
            ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")

        return TemplateOut(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            system_prompt=row["system_prompt"],
            user_prompt_template=row["user_prompt_template"],
            required_sections=json.loads(row["required_sections"] or "[]"),
            is_builtin=False,
            is_modified=False,
        )

    @app.put("/api/templates/{template_id}", response_model=TemplateOut)
    def update_template(template_id: str, payload: TemplateUpdate) -> TemplateOut:
        """Update a template (customizes built-in or updates custom)."""
        # Get the original template (either built-in or custom)
        original: PromptTemplate | None = None
        is_builtin = False

        try:
            original = get_prompt_template(template_id)
            is_builtin = True
        except KeyError:
            pass

        # Check if custom template exists
        with _get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM custom_templates WHERE id = ?",
                (template_id,)
            ).fetchone()

        if not original and not existing:
            raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")

        # Prepare update data
        now = _utcnow_iso()
        name = payload.name if payload.name is not None else (existing["name"] if existing else (original.name if original else ""))
        description = payload.description if payload.description is not None else (existing["description"] if existing else (original.description if original else ""))
        system_prompt = payload.system_prompt if payload.system_prompt is not None else (existing["system_prompt"] if existing else (original.system_prompt if original else ""))
        user_prompt_template = payload.user_prompt_template if payload.user_prompt_template is not None else (existing["user_prompt_template"] if existing else (original.user_prompt_template if original else ""))
        required_sections_json = json.dumps(payload.required_sections) if payload.required_sections is not None else (existing["required_sections"] if existing else json.dumps(list(original.required_sections) if original else []))

        with _get_conn() as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE custom_templates
                    SET name = ?, description = ?, system_prompt = ?, user_prompt_template = ?, required_sections = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, description, system_prompt, user_prompt_template, required_sections_json, now, template_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO custom_templates (id, name, description, system_prompt, user_prompt_template, required_sections, is_builtin, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (template_id, name, description, system_prompt, user_prompt_template, required_sections_json, 1 if is_builtin else 0, now, now),
                )
            conn.commit()

        return TemplateOut(
            id=template_id,
            name=name,
            description=description,
            system_prompt=system_prompt,
            user_prompt_template=user_prompt_template,
            required_sections=json.loads(required_sections_json),
            is_builtin=is_builtin,
            is_modified=True,
        )

    @app.post("/api/templates", response_model=TemplateOut, status_code=201)
    def create_template(payload: TemplateCreate) -> TemplateOut:
        """Create a new custom template."""
        # Check if ID conflicts with built-in
        try:
            get_prompt_template(payload.id)
            raise HTTPException(status_code=400, detail=f"Template ID '{payload.id}' conflicts with built-in template")
        except KeyError:
            pass

        # Check if ID already exists
        with _get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM custom_templates WHERE id = ?",
                (payload.id,)
            ).fetchone()

        if existing:
            raise HTTPException(status_code=400, detail=f"Template with ID '{payload.id}' already exists")

        now = _utcnow_iso()
        required_sections_json = json.dumps(payload.required_sections)

        with _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO custom_templates (id, name, description, system_prompt, user_prompt_template, required_sections, is_builtin, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (payload.id, payload.name, payload.description, payload.system_prompt, payload.user_prompt_template, required_sections_json, now, now),
            )
            conn.commit()

        return TemplateOut(
            id=payload.id,
            name=payload.name,
            description=payload.description,
            system_prompt=payload.system_prompt,
            user_prompt_template=payload.user_prompt_template,
            required_sections=payload.required_sections,
            is_builtin=False,
            is_modified=False,
        )

    @app.delete("/api/templates/{template_id}")
    def delete_template(template_id: str) -> dict[str, str]:
        """Delete a custom template or reset a built-in template."""
        # Check if it's a built-in template
        is_builtin = False
        try:
            get_prompt_template(template_id)
            is_builtin = True
        except KeyError:
            pass

        with _get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM custom_templates WHERE id = ?",
                (template_id,)
            ).fetchone()

        if is_builtin:
            # For built-in templates, delete the customization (reset to default)
            if existing:
                with _get_conn() as conn:
                    conn.execute("DELETE FROM custom_templates WHERE id = ?", (template_id,))
                return {"message": f"Template '{template_id}' reset to default"}
            else:
                raise HTTPException(status_code=400, detail="Cannot delete built-in templates")

        if not existing:
            raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")

        with _get_conn() as conn:
            conn.execute("DELETE FROM custom_templates WHERE id = ?", (template_id,))
            conn.commit()

        return {"message": f"Template '{template_id}' deleted"}

    @app.post("/api/templates/{template_id}/reset")
    def reset_template(template_id: str) -> dict[str, str]:
        """Reset a built-in template to its default values."""
        try:
            get_prompt_template(template_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Built-in template not found: {template_id}")

        with _get_conn() as conn:
            conn.execute("DELETE FROM custom_templates WHERE id = ?", (template_id,))

        return {"message": f"Template '{template_id}' reset to default"}

    # =========================================================================
    # Test API
    # =========================================================================

    @app.post("/api/test/llm")
    def api_test_llm(payload: LLMTestRequest) -> dict[str, Any]:
        """Test LLM connection."""
        return test_llm_connection(payload)

    @app.post("/api/test/webhook")
    def api_test_webhook(payload: WebhookTestRequest) -> dict[str, Any]:
        """Test webhook."""
        target_url = payload.url
        target_type = (payload.type or "discord").strip().lower()
        if payload.webhook_id is not None:
            with _get_conn() as conn:
                row = conn.execute("SELECT type, url FROM webhooks WHERE id = ?", (payload.webhook_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Webhook not found: {payload.webhook_id}")
            target_url = row["url"]
            target_type = str(row["type"] or "discord").strip().lower()
        if not target_url:
            raise HTTPException(status_code=400, detail="Either webhook_id or url required")

        ok, status_code, detail = _send_webhook(target_type, target_url, payload.content)
        return {
            "success": ok,
            "type": target_type,
            "status_code": status_code,
            "detail": detail,
        }

    @app.post("/api/test/miniflux")
    def api_test_miniflux(
        payload: MinifluxTestRequest = Body(default=MinifluxTestRequest()),
    ) -> dict[str, Any]:
        """Test Miniflux connection."""
        return test_miniflux_connection(payload)

    # =========================================================================
    # Error Handlers
    # =========================================================================

    @app.exception_handler(HTTPException)
    def http_exception_handler(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
        )

    return app
