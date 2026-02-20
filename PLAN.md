# OpusBrief - 技术实现计划

> 最后更新: 2026-02-20

## 项目概述

**OpusBrief** 是一个基于 Miniflux RSS 的智能新闻简报系统。

**核心流程**：
```
Miniflux RSS → 筛选分组/时间 → LLM 分析/总结 → Discord 推送
```

---

## 用户背景

Financial Advisor，主要工作：
1. 帮助 AI Startup 从 VC 融资（关注中美 AI 投融资市场）
2. 帮助美国 VC 处理被投公司股权出售（二级市场）

---

## 功能模块

### 1. 四种简报模板

| 模板 | ID | 目标 | 内容侧重 |
|------|-----|------|----------|
| 通用格式 | `general` | FA 日常信息流 | 融资动态 + AI 产品 + 二级市场 |
| 投融资格式 | `investment` | 融资情报 | 轮次/金额/投资方/赛道，表格化 |
| AI 产品格式 | `ai_product` | 产品情报 | 模型发布、新产品、赛道探索、大厂动向 |
| 公众号格式 | `wechat_mp` | 创作灵感 | 焦虑点/共鸣点/争议点/爆款潜力 |

### 2. LLM 提供商

| 提供商 | ID | Base URL |
|--------|-----|----------|
| OpenAI | `openai` | `https://api.openai.com/v1` |
| Anthropic | `anthropic` | `https://api.anthropic.com/v1` |
| SiliconFlow | `siliconflow` | `https://api.siliconflow.cn/v1` |
| 智谱国内 | `zhipu_cn` | `https://open.bigmodel.cn/api/paas/v4` |
| 智谱国际 | `zhipu_us` | `https://open.bigmodel.us/api/paas/v4` |
| Groq | `groq` | `https://api.groq.com/openai/v1` |
| DeepSeek | `deepseek` | `https://api.deepseek.com/v1` |
| 自定义 | `custom` | 用户填写 |

### 3. 时区支持

- `Asia/Shanghai` - 北京
- `America/New_York` - 纽约
- `America/Los_Angeles` - 洛杉矶
- `Europe/London` - 伦敦

### 4. 推送平台

- Discord Webhook（优先）
- 企业微信群机器人（待定）

---

## 项目结构

```
OpusSummary/
├── main.py              # 入口
├── database.py          # SQLite 数据库
├── miniflux_client.py   # Miniflux API 客户端
│
├── briefing/            # 简报模块
│   ├── __init__.py
│   ├── generator.py     # 简报生成核心
│   ├── templates.py     # 四种模板 Prompt
│   └── scheduler.py     # 定时任务
│
├── llm/                 # LLM 模块
│   ├── __init__.py
│   ├── providers.py     # 提供商配置
│   └── client.py        # 统一客户端
│
├── sender/              # 推送模块
│   ├── __init__.py
│   └── discord.py       # Discord Webhook
│
├── web/                 # Web UI
│   ├── __init__.py
│   ├── server.py        # FastAPI
│   └── static/          # 前端
│
├── data/                # 数据存储
│   └── briefings.db
│
├── requirements.txt
└── PLAN.md
```

---

## 数据库 Schema

```sql
-- LLM 配置
CREATE TABLE llm_configs (
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
CREATE TABLE webhooks (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    type        TEXT,
    url         TEXT,
    is_default  BOOLEAN DEFAULT 0,
    created_at  TEXT
);

-- 简报任务
CREATE TABLE briefing_tasks (
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
CREATE TABLE briefings (
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
CREATE TABLE articles (
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
CREATE TABLE user_preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
```

---

## 实现步骤

### Phase 1: 基础架构 ✅
- [x] 1.1 创建数据库模块 (`database.py`)
- [x] 1.2 调整 Miniflux 客户端 (`miniflux_client.py`)
- [x] 1.3 创建 LLM 提供商配置 (`llm/providers.py`)
- [x] 1.4 创建 LLM 统一客户端 (`llm/client.py`)

### Phase 2: 简报核心 ✅
- [x] 2.1 设计四种模板 Prompt (`briefing/templates.py`)
- [x] 2.2 实现简报生成器 (`briefing/generator.py`)
- [x] 2.3 实现定时调度器 (`briefing/scheduler.py`)

### Phase 3: 推送 ✅
- [x] 3.1 Discord Webhook 推送 (`sender/discord.py`)

### Phase 4: Web UI ✅
- [x] 4.1 FastAPI 服务端 (`web/server.py`)
- [x] 4.2 配置页面
- [x] 4.3 任务管理页面
- [x] 4.4 手动生成页面
- [x] 4.5 历史记录页面

### Phase 5: 集成 ✅
- [x] 5.1 主入口 (`main.py`)
- [x] 5.2 测试与调试

---

## 实现完成情况

> 完成时间: 2026-02-20

### 功能测试结果

| 功能 | 状态 | 备注 |
|------|------|------|
| Miniflux 连接 | ✅ 通过 | 成功获取 18 个分组 |
| LLM 连接测试 | ✅ 通过 | SiliconFlow API 正常 |
| 通用格式简报 | ✅ 通过 | 6 篇文章，status=success |
| 投融资格式简报 | ✅ 通过 | 49 篇文章，status=success |
| AI产品格式简报 | ✅ 通过 | 9 篇文章，status=success |
| 公众号格式简报 | ✅ 通过 | 6 篇文章，status=success |
| Web UI | ✅ 运行 | http://127.0.0.1:8092 |
| REST API | ✅ 正常 | 所有端点可用 |

### 已修复的问题

1. **Token 超限** - 限制文章内容长度和总字符数
2. **LLM 集成** - 成功集成 BriefingGenerator 和 LLMClient
3. **安全增强** - CORS 限制为 localhost，添加 URL 验证
4. **输入验证** - 增强 category_ids 和 webhook_ids 校验
5. **重复方法定义** - 修复 LLMClient.complete() 方法被定义两次的问题 (2026-02-20)

### 代码审查记录 (Round 3)

| 审查项 | 状态 | 说明 |
|--------|------|------|
| 并发安全 | ✅ 通过 | SQLite WAL 模式，连接上下文管理正确 |
| 资源管理 | ✅ 通过 | HTTP 客户端和数据库连接正确关闭 |
| 异常处理 | ✅ 通过 | 完整的异常层次结构，重试机制完善 |
| 日志记录 | ✅ 通过 | 关键操作有完整日志 |

### 待完善功能

- [ ] 定时任务自动执行（调度器已实现，需在服务启动时激活）
- [ ] 企业微信群机器人推送
- [ ] API Key 加密存储
- [ ] 安装 markdown 库以支持 HTML 输出

---

## 启动命令

```bash
python main.py              # 启动 Web UI + 定时任务
python main.py web          # 仅 Web UI
python main.py generate     # 手动生成简报
python main.py test-llm     # 测试 LLM 连接
python main.py test-miniflux # 测试 Miniflux 连接
```
