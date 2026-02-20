"""
Opus Relay - Web 配置管理 API

基于 FastAPI 提供 REST API + WebSocket，用于：
- 读取和保存 config.json
- 测试 Miniflux / 翻译 / Discord 连接
- 获取 Feed 分组列表
- WebSocket 实时日志推送
- 配置热重载（通知 scheduler 更新）
"""

import json
import os
import logging
import asyncio
from datetime import datetime
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx

logger = logging.getLogger("opus.web")

# 配置文件路径
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATIC_DIR = os.path.join(CONFIG_DIR, "static")

app = FastAPI(title="Opus Relay 配置管理", version="1.0")


# ========== WebSocket 日志广播 ==========

class WebSocketLogManager:
    """管理 WebSocket 连接和日志广播"""

    def __init__(self):
        self.connections: list[WebSocket] = []
        self._log_buffer: list[dict] = []   # 最近日志缓冲（供新连接回看）
        self._buffer_max = 200

    async def connect(self, ws: WebSocket):
        """接受新连接并发送缓冲日志"""
        await ws.accept()
        self.connections.append(ws)
        # 发送缓冲日志，让新连接能看到历史
        for log in self._log_buffer:
            try:
                await ws.send_json(log)
            except Exception:
                break

    def disconnect(self, ws: WebSocket):
        """移除断开的连接"""
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, log_entry: dict):
        """向所有连接广播日志"""
        self._log_buffer.append(log_entry)
        if len(self._log_buffer) > self._buffer_max:
            self._log_buffer = self._log_buffer[-self._buffer_max:]

        disconnected = []
        for ws in self.connections:
            try:
                await ws.send_json(log_entry)
            except Exception:
                disconnected.append(ws)

        for ws in disconnected:
            self.disconnect(ws)


# 全局日志管理器
log_manager = WebSocketLogManager()


class WebSocketLogHandler(logging.Handler):
    """将 Python 日志转发到 WebSocket"""

    def __init__(self, manager: WebSocketLogManager):
        super().__init__()
        self.manager = manager
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """设置事件循环引用"""
        self._loop = loop

    def emit(self, record: logging.LogRecord):
        if self._loop is None or self._loop.is_closed():
            return

        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }

        try:
            asyncio.run_coroutine_threadsafe(
                self.manager.broadcast(log_entry),
                self._loop,
            )
        except Exception:
            pass  # 忽略广播失败


# 全局日志处理器
ws_handler = WebSocketLogHandler(log_manager)
ws_handler.setLevel(logging.INFO)


# ========== Scheduler 引用（供热重载使用） ==========

_scheduler = None  # 运行时由 main.py 注入


def set_scheduler(scheduler):
    """设置 scheduler 引用，用于热重载"""
    global _scheduler
    _scheduler = scheduler


# ========== 生命周期 ==========

@app.on_event("startup")
async def on_startup():
    """FastAPI 启动时：挂载 WebSocket 日志处理器"""
    loop = asyncio.get_event_loop()
    ws_handler.set_loop(loop)

    # 挂载到根 logger，捕获所有 opus.* 日志
    root_logger = logging.getLogger("opus")
    root_logger.addHandler(ws_handler)

    # 也捕获 httpx 日志（API 请求）
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addHandler(ws_handler)

    logger.info("WebSocket 日志广播已启动")


# ========== 数据模型 ==========

class TranslationConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"


class AppConfig(BaseModel):
    miniflux_url: str = ""
    miniflux_token: str = ""
    poll_interval_minutes: int = 5
    batch_interval_seconds: int = 120
    batch_max_items: int = 15
    timezone: str = "UTC"
    content_summary_length: int = 300
    categories_with_content: list[str] = []
    translation: TranslationConfig = TranslationConfig()
    routes: dict[str, str] = {}


class TestMinifluxRequest(BaseModel):
    url: str
    token: str


class TestTranslationRequest(BaseModel):
    base_url: str
    api_key: str
    model: str = "gpt-4o-mini"


class TestWebhookRequest(BaseModel):
    webhook_url: str


class CategoryContentRequest(BaseModel):
    enabled: bool


# ========== WebSocket 端点 ==========

@app.websocket("/ws/logs")
async def websocket_logs(ws: WebSocket):
    """实时日志 WebSocket 端点"""
    await log_manager.connect(ws)
    try:
        while True:
            # 保持连接，接收客户端心跳
            await ws.receive_text()
    except WebSocketDisconnect:
        log_manager.disconnect(ws)


# ========== 配置读写 API ==========

@app.get("/api/config")
async def get_config():
    """读取当前配置"""
    if not os.path.exists(CONFIG_PATH):
        return AppConfig().model_dump()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")


@app.post("/api/config")
async def save_config(config: AppConfig):
    """保存配置到 config.json 并热重载"""
    try:
        config_dict = config.model_dump()
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)

        # 热重载：通知 scheduler 更新配置
        if _scheduler is not None:
            try:
                _scheduler.reload(config_dict)
                return {"success": True, "message": "配置已保存并热重载生效 ✅"}
            except Exception as e:
                logger.error(f"热重载失败: {e}")
                return {"success": True, "message": f"配置已保存，但热重载失败: {e}（需手动重启）"}
        else:
            return {"success": True, "message": "配置已保存（轮询服务未运行，重启后生效）"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {e}")


@app.get("/api/status")
async def get_status():
    """获取服务状态"""
    return {
        "scheduler_running": _scheduler is not None,
        "connections": len(log_manager.connections),
    }


# ========== 测试连接 API ==========

@app.post("/api/test/miniflux")
async def test_miniflux(req: TestMinifluxRequest):
    """测试 Miniflux 连接"""
    url = req.url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{url}/v1/me",
                headers={"X-Auth-Token": req.token},
            )
            if resp.status_code == 200:
                user = resp.json().get("username", "未知")
                return {"success": True, "message": f"连接成功，用户: {user}"}
            else:
                return {"success": False, "message": f"连接失败: HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {e}"}


@app.post("/api/test/translation")
async def test_translation(req: TestTranslationRequest):
    """测试翻译 API"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{req.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {req.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": req.model,
                    "messages": [
                        {"role": "system", "content": "将文章标题翻译为中文，只返回翻译结果。"},
                        {"role": "user", "content": "OpenAI Releases GPT-5 with Breakthrough Capabilities"},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                translated = data["choices"][0]["message"]["content"].strip().strip("\"'")
                return {
                    "success": True,
                    "message": "翻译成功",
                    "original": "OpenAI Releases GPT-5 with Breakthrough Capabilities",
                    "translated": translated,
                    "model": req.model,
                }
            else:
                return {"success": False, "message": f"API 错误: HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"翻译测试失败: {e}"}


@app.post("/api/test/webhook")
async def test_webhook(req: TestWebhookRequest):
    """测试 Discord Webhook（发送一条测试消息）"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                req.webhook_url,
                json={
                    "embeds": [{
                        "title": "🔔 Opus Relay 测试消息",
                        "description": "如果你看到这条消息，说明 Webhook 配置正确！",
                        "color": 5763719,  # 绿色
                    }],
                },
            )
            if resp.status_code == 204:
                return {"success": True, "message": "Webhook 测试成功，请检查 Discord 频道"}
            elif resp.status_code == 404:
                return {"success": False, "message": "Webhook URL 无效（404）"}
            else:
                return {"success": False, "message": f"发送失败: HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"Webhook 测试失败: {e}"}


# ========== Feed 列表 API ==========

@app.get("/api/feeds")
async def get_feeds():
    """获取 Miniflux 的 Feed 分组列表"""
    if not os.path.exists(CONFIG_PATH):
        raise HTTPException(status_code=400, detail="请先配置 Miniflux 连接信息")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    url = config.get("miniflux_url", "").rstrip("/")
    token = config.get("miniflux_token", "")
    categories_with_content = set(config.get("categories_with_content", []))

    if not url or not token:
        raise HTTPException(status_code=400, detail="Miniflux URL 或 Token 未配置")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{url}/v1/feeds",
                headers={"X-Auth-Token": token},
            )
            resp.raise_for_status()
            feeds = resp.json()

            # 按分组聚合
            categories = {}
            for feed in feeds:
                cat = feed.get("category", {})
                cat_id = str(cat.get("id", "0"))
                cat_name = cat.get("title", "未分组")

                if cat_id not in categories:
                    categories[cat_id] = {
                        "id": cat_id,
                        "name": cat_name,
                        "feeds": [],
                        "content_enabled": cat_id in categories_with_content,
                    }
                categories[cat_id]["feeds"].append({
                    "id": feed.get("id"),
                    "title": feed.get("title", "无标题"),
                })

            result = sorted(categories.values(), key=lambda c: c["name"])
            return {"categories": result, "total_feeds": len(feeds)}

    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"获取 Feed 列表失败: {e}")


# ========== 分组 Content 配置 API ==========

@app.put("/api/categories/{category_id}/content")
async def update_category_content(category_id: str, req: CategoryContentRequest):
    """更新分组的 content 获取配置"""
    if not os.path.exists(CONFIG_PATH):
        raise HTTPException(status_code=400, detail="配置文件不存在")

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

        categories_with_content = set(config.get("categories_with_content", []))

        if req.enabled:
            categories_with_content.add(category_id)
        else:
            categories_with_content.discard(category_id)

        config["categories_with_content"] = list(categories_with_content)

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        # 热重载
        if _scheduler is not None:
            try:
                _scheduler.reload(config)
            except Exception as e:
                logger.error(f"热重载失败: {e}")

        return {"success": True, "enabled": req.enabled}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新配置失败: {e}")


# ========== 静态文件 & 首页 ==========

os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    """返回配置页面"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
