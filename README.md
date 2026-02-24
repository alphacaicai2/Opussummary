# 📡 Opus Relay

Miniflux RSS → Discord 智能推送服务

**功能特性：**

- 🔄 定时轮询 Miniflux 未读文章，增量拉取
- 🌐 AI 翻译英文标题为中文（OpenAI 兼容接口）
- 🎯 按 RSS 分组路由到不同 Discord 频道
- 📦 批量队列发送，自动限流
- 🖥️ Web 配置管理 UI（深色主题）
- 📝 实时日志面板（WebSocket）
- ♻️ 配置热重载，无需重启

---

## 快速开始

### 1. 安装依赖

```bash
uv pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.json config.json
# 编辑 config.json 填入你的 Miniflux、翻译 API 和 Discord Webhook 信息
```

或者启动后通过 Web UI 配置：

### 2.1 启用飞书登录（可选）

在 `.env` 中加入以下变量即可启用 Web 管理端飞书登录：

```bash
FEISHU_LOGIN_ENABLED=true
FEISHU_CLIENT_ID=cli_xxx
FEISHU_APP_SECRET=xxxx
FEISHU_SESSION_SECRET=请填写长度足够的随机字符串
# 兼容旧命名：FEISHU_APP_ID（如同时存在，优先读取 FEISHU_CLIENT_ID）
# 可选：固定回调地址（不填则自动按请求域名拼接 /auth/feishu/callback）
# FEISHU_REDIRECT_URI=https://smart.vibexcap.com/auth/feishu/callback
# 可选：限制允许登录的用户（支持 open_id/user_id/union_id/email）
# FEISHU_ALLOWED_IDENTIFIERS=alice@vibexcap.com,bob@vibexcap.com
# 可选：按邮箱域名限制
# FEISHU_ALLOWED_EMAIL_DOMAINS=vibexcap.com
```

飞书开放平台里需要把回调地址配置为：

`https://smart.vibexcap.com/auth/feishu/callback`

### 3. 运行

```bash
uv run python main.py          # Web UI + 轮询服务（默认）
```

访问 **http://localhost:8090** 管理配置、查看实时日志。

### 其他命令

```bash
uv run python main.py web              # 仅 Web UI（不轮询）
uv run python main.py poll-only        # 仅轮询（无 Web UI）
uv run python main.py poll-once        # 执行一次轮询
uv run python main.py test-translation # 测试翻译 API
uv run python main.py test-miniflux    # 测试 Miniflux 连接
uv run python main.py list-feeds       # 列出所有 Feed 分组
```

---

## Docker 部署

```bash
docker compose up -d
```

---

## 版本号自动更新

项目使用根目录 `VERSION` 作为统一版本源。  
每次 `git commit` 会自动执行 patch 递增（例如 `0.1.0 -> 0.1.1`）。

首次拉取后请在仓库执行：

```bash
git config core.hooksPath .githooks
```

如需临时跳过（不建议），可设置：

```bash
OPUS_SKIP_VERSION_BUMP=1 git commit -m "your message"
```

---

## 配置说明

```json
{
  "miniflux_url": "https://miniflux.example.com",
  "miniflux_token": "YOUR_API_TOKEN",
  "poll_interval_minutes": 5,
  "batch_interval_seconds": 120,
  "batch_max_items": 15,
  "translation": {
    "base_url": "https://api.siliconflow.cn/v1",
    "api_key": "sk-xxx",
    "model": "Qwen/Qwen2.5-7B-Instruct"
  },
  "routes": {
    "3": "https://discord.com/api/webhooks/xxx/yyy"
  }
}
```

| 字段                     | 说明                               |
| ------------------------ | ---------------------------------- |
| `miniflux_url`           | Miniflux 实例地址                  |
| `miniflux_token`         | Miniflux API Token                 |
| `poll_interval_minutes`  | 轮询间隔（分钟）                   |
| `batch_interval_seconds` | 批量发送间隔（秒）                 |
| `batch_max_items`        | 每批最大发送条数                   |
| `translation`            | AI 翻译配置（留空则不翻译）        |
| `routes`                 | 分组 ID → Discord Webhook URL 映射 |

---

## 架构

```
Miniflux ──→ miniflux_client.py ──→ scheduler.py ──→ discord_sender.py ──→ Discord
                                        │
                                   translator.py (AI 翻译)
                                   database.py   (SQLite 去重)
                                        │
                                   web_server.py ──→ WebSocket 实时日志
                                   static/        ──→ 配置管理 UI
```

## 技术栈

- **Python 3.12+**、httpx、FastAPI、uvicorn、WebSocket
- **SQLite** 去重数据库
- **Miniflux API** / **OpenAI 兼容翻译 API** / **Discord Webhooks**
